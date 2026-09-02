import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import os
import glob

def extract_stim_timestamps_and_values(filepath):
    syncOn_behav = []
    syncOff_behav = []
    trial_times = []
    lick_events = []
    sample_tone=[]
    ITIs = []
    with open(filepath, 'r') as file:
        for line in file:
            if 'Key: Trial ' in line and 'Value: Start' in line:
                # Split the line by 'Timestamp(ms): ' and extract the timestamp
                parts = line.strip().split('Timestamp(ms): ')
                if len(parts) > 1:
                    timestamp = float(parts[1])/1000
                    trial_times.append((timestamp))
            elif 'Key: Lick Port 1' in line and 'Value: Touched' in line:
                # Split the line by 'Timestamp(ms): ' and extract the timestamp
                parts = line.strip().split('Timestamp(ms): ')
                if len(parts) > 1:
                    timestamp = float(parts[1])
                    lick_events.append((timestamp, 1))
            elif 'Key: Lick Port 2' in line and 'Value: Touched' in line:
                # Split the line by 'Timestamp(ms): ' and extract the timestamp
                parts = line.strip().split('Timestamp(ms): ')
                if len(parts) > 1:
                    timestamp = float(parts[1])
                    lick_events.append((timestamp, 2))
            elif 'Key: Freq' in line:
                value_part = line.split('Value: ')[1].split(',')[0]
                sample_tone.append(float(value_part))
            elif 'Key: ITI' in line:
                value_part = line.split('Value: ')[1].split(',')[0]
                ITIs.append(float(value_part)/1000)
            elif 'Subject ID:' in line:
                subject_ID = line.split('Subject ID:')[1].strip()
            elif 'Start Time:' in line:
                start_time_str = line.split('Start Time:')[1].strip()
                start_time = pd.to_datetime(start_time_str, format='%d-%b-%Y %H:%M:%S').strftime('%Y%m%d_%H%M%S')
            elif 'Key: Barcode' in line and 'Value: On' in line:
                parts = line.strip().split('Timestamp(ms): ')
                if len(parts) > 1:
                    timestamp = float(parts[1]) / 1000
                    syncOn_behav.append(timestamp)
            elif 'Key: Barcode' in line and 'Value: Off' in line:
                parts = line.strip().split('Timestamp(ms): ')
                if len(parts) > 1:
                    timestamp = float(parts[1]) / 1000
                    syncOff_behav.append(timestamp)
    return trial_times, lick_events, subject_ID, start_time, syncOn_behav, syncOff_behav, ITIs

def find_file_with_pattern(folder, pattern):
    """
    Search for the first file matching the pattern in the folder and its subfolders.
    Returns the file path if found, else None.
    """
    matches = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if glob.fnmatch.fnmatch(file, pattern):
                matches.append(os.path.join(root, file))
    if matches:
        if len(matches) > 1:
            print(f"Warning: Multiple files found for pattern '{pattern}'. Using {os.path.basename(matches[0])}")
        return matches[0]
    else:
        return None
    
def extract_camera_logFile(filepath):
    syncOn_camera = []
    syncOff_camera = []
    frame_times = []

    with open(filepath, 'r') as file:
        for line in file:
            parts = line.strip().split(',')
            if len(parts) == 3:
                key, value, timestamp = parts[0], parts[1], float(parts[2])
                if key == 'Frame Number':
                    frame_times.append(timestamp / 1000)
                elif key == 'Barcode' and value == 'On':
                    syncOn_camera.append(timestamp / 1000)
                elif key == 'Barcode' and value == 'Off':
                    syncOff_camera.append(timestamp / 1000)

    return syncOn_camera, syncOff_camera, frame_times
    
def extract_barcodes_from_times(on_times, off_times, inter_barcode_interval=5, bar_duration=0.03, 
                                barcode_duration_ceiling=2, nbits=32):
    start_indices = np.diff(on_times)
    a = np.where(start_indices > inter_barcode_interval)[0]
    barcode_start_times = on_times[a + 1]
    barcodes = []
    for t in barcode_start_times:
        try:
            oncode = on_times[(on_times > t) & (on_times < t + barcode_duration_ceiling)]
            offcode = off_times[(off_times > t) & (off_times < t + barcode_duration_ceiling)]
            curr_time = offcode[0] + 0.010
            count = 0
            bits = []
            bit_count = round((oncode[0] - curr_time) / bar_duration)
            bits.extend([0] * bit_count)
            count += bit_count
            for i in range(len(offcode) - 2):
                bit_count = round((offcode[i + 1] - oncode[i]) / bar_duration)
                bits.extend([1] * bit_count)
                count += bit_count
                bit_count = round((oncode[i + 1] - offcode[i + 1]) / bar_duration)
                bits.extend([0] * bit_count)
                count += bit_count
            bit_count = round((offcode[-1] - oncode[-1]) / bar_duration)
            bits.extend([1] * bit_count)
            count += bit_count
            binary_number = int(''.join(map(str, bits[::-1])), 2)
        except Exception:
            binary_number = float('nan')
        barcodes.append(binary_number)
    return barcode_start_times, barcodes

def calculate_psth(events, stim_times, bin_size=0.1, window=(-2, 10)):
    bins = np.arange(window[0], window[1] + bin_size, bin_size)
    bin_centers = bins[:-1]
    trial_psth = np.zeros((len(stim_times), len(bins) - 1))
    
    for i, stim_time in enumerate(stim_times):
        aligned_events = [event - stim_time for event in events if window[0] <= (event - stim_time) <= window[1]]
        hist, _ = np.histogram(aligned_events, bins=bins)
        trial_psth[i, :] = hist / bin_size  # Normalize by bin size for each trial
    
    # Calculate average PSTH across all trials
    avg_psth = np.mean(trial_psth, axis=0)
    
    return bin_centers, avg_psth, trial_psth

def smooth_psth(psth, alpha=0.2):
    """
    Apply exponential smoothing to PSTH data.
    Works with both 1D arrays (average PSTH) and 2D arrays (trial-by-trial PSTH).
    
    Args:
        psth: 1D array (average PSTH) or 2D array (trials x time bins)
        alpha: Smoothing factor
        
    Returns:
        Smoothed version of the input array with same dimensions
    """
    if psth.ndim == 1:
        # Original 1D smoothing
        smoothed_psth = np.zeros_like(psth)
        smoothed_psth[0] = psth[0]
        for i in range(1, len(psth)):
            smoothed_psth[i] = alpha * psth[i] + (1 - alpha) * smoothed_psth[i - 1]
        return smoothed_psth
    elif psth.ndim == 2:
        # 2D case: smooth each trial (row) separately
        smoothed_psth = np.zeros_like(psth)
        for trial in range(psth.shape[0]):
            smoothed_psth[trial, 0] = psth[trial, 0]
            for i in range(1, psth.shape[1]):
                smoothed_psth[trial, i] = alpha * psth[trial, i] + (1 - alpha) * smoothed_psth[trial, i - 1]
        return smoothed_psth
    else:
        raise ValueError("Input PSTH must be either 1D or 2D array")

def plot_psth(ax, bins, psth, smoothed_psth, title, max_y):
    ax.bar(bins, psth, width=0.1, align='edge', alpha=0.5, label='Bar Graph')
    ax.plot(bins, smoothed_psth, color='r', label='Smoothed PSTH')
    ax.set_title(title)
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Lick Rate (licks/s)')
    ax.set_ylim(0, max_y)
    ax.axvline(0, color='k', linestyle='--')
    ax.legend()

def generate_raster(events, stim_times, window=(-2, 10)):
    raster_data = []
    for stim_time in stim_times:
        aligned_events = [(event - stim_time)  for event in events if window[0] <= (event - stim_time)  <= window[1]]
        raster_data.append(aligned_events)
    return raster_data

def plot_raster(ax, raster_data, title):
    for i, trial in enumerate(raster_data):
        ax.vlines(trial, i + 0.5, i + 1.5)
    ax.set_title(title)
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Trial')
    ax.axvline(0, color='k', linestyle='--')


def find_subfolder_with_phrase(parent_folder, phrase):
    """
    Find a subfolder within the parent folder that contains the specified phrase.
    
    Args:
        parent_folder (str): Path to the parent folder
        phrase (str): The phrase to search for in subfolder names
    
    Returns:
        str: Full path to the selected subfolder, or None if no matching subfolder is found
    """
    matching_subfolders = []
    for root, dirs, files in os.walk(parent_folder):
        for dir_name in dirs:
            if phrase.lower() in dir_name.lower():
                matching_subfolders.append(os.path.join(root, dir_name))
    
    if not matching_subfolders:
        print(f"No subfolders containing '{phrase}' found. Exiting.")
        return None
        
    # If multiple matching folders found, let the user select one
    if len(matching_subfolders) > 1:
        print(f"Multiple {phrase} folders found. Please select one:")
        for i, folder in enumerate(matching_subfolders):
            print(f"{i+1}. {folder}")
        
        selection = input("Enter the number of your selection: ")
        try:
            return matching_subfolders[int(selection) - 1]
        except (ValueError, IndexError):
            print("Invalid selection. Exiting.")
            return None
    else:
        selected_folder = matching_subfolders[0]
        print(f"Using {phrase} folder: {selected_folder}")
        return selected_folder
