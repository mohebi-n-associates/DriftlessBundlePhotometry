import os
import numpy as np
import matplotlib.pyplot as plt

root_folder = "/Volumes/amohebi/Robert Spencer/Animals/IM-1020/2024-04-15/Imaging/IM-1020_2025-04-15_12-40-09"


def compute_frame_averages(root_folder, image_shape=(512, 612), dtype=np.uint16):
    # First, collect all frame numbers
    frame_numbers_list = []
    for dirpath, dirnames, filenames in os.walk(root_folder):
        files = [f for f in filenames if f.startswith("image_") and f.endswith(".bin")]
        for filename in files:
            frame_number = int(filename.split('_')[1].split('.')[0])
            frame_numbers_list.append(frame_number)

    frame_numbers = np.array(sorted(frame_numbers_list), dtype=int)
    averages = np.full_like(frame_numbers, np.nan, dtype=np.float32)

    # Map frame number to index for fast lookup
    frame_to_index = {fn: idx for idx, fn in enumerate(frame_numbers)}

    processed = 0
    for dirpath, dirnames, filenames in os.walk(root_folder):
        files = sorted([f for f in filenames if f.startswith("image_") and f.endswith(".bin")])
        for filename in files:
            frame_number = int(filename.split('_')[1].split('.')[0])
            filepath = os.path.join(dirpath, filename)
            with open(filepath, "rb") as f:
                img = np.frombuffer(f.read(), dtype=dtype).reshape(image_shape)
            avg = img.mean()
            idx = frame_to_index[frame_number]
            averages[idx] = avg
            processed += 1
            if processed % 100 == 0:
                print(f"Processed {processed} files...")
    return frame_numbers, averages, processed

def causal_smooth(data, window_size):
    """Apply causal moving average smoothing."""
    smoothed = np.full_like(data, np.nan, dtype=np.float32)
    for i in range(len(data)):
        if i < window_size - 1:
            smoothed[i] = np.nanmean(data[:i+1])
        else:
            smoothed[i] = np.nanmean(data[i-window_size+1:i+1])
    return smoothed

folder_color_pairs = [
    ("405/8", "magenta"),
    ("470/8", "green"),
    ("565/8", "red"),
]

results = {}

# First loop: compute and store results
for folder, color in folder_color_pairs:
    subfolder = os.path.join(root_folder, folder)
    frame_numbers, averages, processed = compute_frame_averages(subfolder)
    print(f"Total files processed in {folder}: {processed}")
    results[folder] = {
        "frame_numbers": frame_numbers,
        "averages": averages,
        "color": color
    }

# Second loop: plot results
for folder in folder_color_pairs:
    folder_name, color = folder
    data = results[folder_name]
    plt.plot(data["frame_numbers"], data["averages"], linestyle='-', color=data["color"], label=folder_name)

plt.xlabel('Frame Number')
plt.ylabel('Average Pixel Value')
plt.title('Average Pixel Value per Frame')
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.show()



window_size = 5  # Adjust as needed

fig, axes = plt.subplots(len(folder_color_pairs), 1, figsize=(8, 4 * len(folder_color_pairs)), sharex=True)
if len(folder_color_pairs) == 1:
    axes = [axes]  # Ensure axes is iterable
for ax, (folder_name, color) in zip(axes, folder_color_pairs):
    data = results[folder_name]
    averages = data["averages"]
    zscores = (averages - np.nanmean(averages)) / np.nanstd(averages)
    zscores_smoothed = causal_smooth(zscores, window_size)
    ax.plot(data["frame_numbers"], zscores_smoothed, linestyle='-', color=color, label=f"{folder_name} (z-score, causal smoothed)")
    ax.set_ylabel('Z-scored Avg Pixel Value')
    ax.set_title(f'{folder_name} (z-score, causal smoothed)')
    ax.legend()
    ax.grid(True)

axes[-1].set_xlabel('Frame Number')
plt.tight_layout()
plt.show()

