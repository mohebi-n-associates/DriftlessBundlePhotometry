# Script for acquiring video data from multiple Point Grey cameras using Bonsai to 
# extract the state of the GPIO pins and pipe video data to FFMPEG for compression.

# The script launches Bonsai with a command which loads the workflow and configures
# workflow variables to acquire data from the specified boxes.  Once Bonsai has 
# opened pipes to send data to FFMPEG, the script launches FFMPEG instances which 
# use the GPU to H264 compress the video data. When the script detects that Bonsai 
# has been closed it closes the FFMPEG instances.

# (c) Thomas Akam 2019-2022.  Licenced under the GNU General Public License v3.

import os
import time

from datetime import datetime
from subprocess import Popen

#from config import subjects, data_dir, bonsai_path, workflow_path, framerate, camera_res, downsample

datetime_str = datetime.now().strftime('%Y-%m-%d-%H%M%S')

subjects = {1:'FaceCam',  # {box: subject_ID} 
            2:'FaceCam'}

#subject_ID = 'IM-xxxx'

#data_dir = 'C:\\Data'  # Directory where data will be saved.

# --------------------------------------------------------------------
# Hardware config.
# --------------------------------------------------------------------


camera_res = (2*1280,1024) # Should be set to match camera setting: (width, height) in pixels.

framerate = 120           # Should be set to match camera settings.

# --------------------------------------------------------------------
# Video format config
# --------------------------------------------------------------------

downsample = 2 #False # Set to a number to spatialy downsample output to lower resolution, 
                   # e.g. downsample=2 will reduce a 1280x1024 input to a 640x512 output.

# --------------------------------------------------------------------
# Bonsai path config.
# --------------------------------------------------------------------

bonsai_path = 'C:\\Bonsai\\Bonsai.exe'

workflow_path = '"C:\\Github\\mesoscope\\concatCameras.bonsai"'

arduinoLog_file_path = os.path.join(data_dir, subject_ID + '_BarCodeTimestamps_' + datetime_str + '.csv')

b_command = f'{bonsai_path} {workflow_path} --start -p BarCodeFileName={arduinoLog_file_path}' # Command to launch Bonsai.

# Append subject specific info to Bonsai launch command.

pinstate_file_path = os.path.join(data_dir, subject_ID + '_FaceCam_pinstate_' + datetime_str + '.csv')
b_command += f' -p PinFileName={pinstate_file_path} '
 
# Launch Bonsai

bonsai_process = Popen(b_command)

# Launch FFMPEG instances once video pipes are open.

output_res = (camera_res[0]//downsample,camera_res[1]//downsample) if downsample else camera_res

pipes_open = [False]

ffmpeg_processes = []

while not all(pipes_open):
    if not pipes_open[0]:
        pipe = r'\\.\pipe\videopipe1'
        if pipe.split('\\')[-1] in os.listdir(r'\\.\pipe'):
            pipes_open[0] = True
            video_file_path = os.path.join(data_dir, subject_ID + '_FaceCam_' + datetime_str + '.mp4')
            ffmpeg_processes.append(Popen(
                fr'ffmpeg -y -f rawvideo -vcodec rawvideo -s {camera_res[0]}x{camera_res[1]} '
                fr'-pix_fmt gray -r {framerate} -i {pipe} -c:v h264_nvenc '
                fr'-profile:v high -preset slow -vf scale={output_res[0]}:{output_res[1]} -an {video_file_path}'
                ))
    time.sleep(.1)


os.system("title " + 'Camera acquisition') # Set name of terminal window.

# Wait untill Bonsai has stopped running.

while bonsai_process.poll() == None:
    time.sleep(1)

print('Closing ffmpeg processes.')

for ffmpeg_process in ffmpeg_processes:
    ffmpeg_process.terminate()
