import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import filedialog

from config import BARCODE_DELAY, BIN_SIZE, WINDOW, ALPHA
from functions import *
import glob

# Get the BASE_FOLDER using a UI folder selection dialog

# Initialize tkinter and hide the main window
root = tk.Tk()
root.withdraw()

# Open file dialog to select the base folder
print("Please select the base folder containing your data:")
BASE_FOLDER = filedialog.askdirectory(title="Select Base Folder")

if not BASE_FOLDER:
    print("No folder selected. Exiting.")
    sys.exit()

# Find the analyzer folder
IMAGING_FOLDER = find_subfolder_with_phrase(BASE_FOLDER, "Imaging")
BEHAVIOR_FOLDER = find_subfolder_with_phrase(BASE_FOLDER, "Behavior")


BEHAVIOR_FILEPATH = find_file_with_pattern(BEHAVIOR_FOLDER, '*.txt')
if BEHAVIOR_FILEPATH is None:
    print("No .txt file found in the Behavior folder. Exiting.")
    sys.exit()

# Find the camera log file in IMAGING_FOLDER and its subfolders
CAMERALOG_FILEPATH = find_file_with_pattern(IMAGING_FOLDER, '*camera_log.txt')
if CAMERALOG_FILEPATH is None:
    print("No camera log file found in the IMAGING folder or its subfolders. Exiting.")
    sys.exit()


FIG_FOLDER = f'{BASE_FOLDER}/figures'
os.makedirs(FIG_FOLDER, exist_ok=True)

trial_times, lick_events, subject_ID, start_time, syncOn_behav, syncOff_behav, ITIs = extract_stim_timestamps_and_values(BEHAVIOR_FILEPATH)
syncOn_camera, syncOff_camera, frame_times = extract_camera_logFile(CAMERALOG_FILEPATH)


barcode_start_times_behavior, barcodes_behavior = extract_barcodes_from_times(np.array(syncOn_behav), np.array(syncOff_behav))
barcode_start_times_camera, barcodes_camera = extract_barcodes_from_times(np.array(syncOn_camera), np.array(syncOff_camera))

first_barcode = barcodes_behavior[BARCODE_DELAY]
last_barcode = barcodes_behavior[-BARCODE_DELAY]
t_first_behavior = barcode_start_times_behavior[BARCODE_DELAY]
t_last_behavior = barcode_start_times_behavior[-BARCODE_DELAY]
# Now frameTime_ms contains the timestamps in milliseconds relative to the start


first_barcode_idx = np.where(np.array(barcode_data_value) == first_barcode)[0][0]
last_barcode_idx = np.where(np.array(barcode_data_value) == last_barcode)[0][0]
t_first_camera = barcode_data_time[first_barcode_idx]
t_last_camera = barcode_data_time[last_barcode_idx]