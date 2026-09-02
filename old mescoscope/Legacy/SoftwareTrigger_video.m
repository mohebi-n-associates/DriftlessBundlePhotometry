clc; clear all; close all;

%% Set the variables
exposure = 20;
outputFolder = "E:\imaging\test";
mkdir(outputFolder)

% Start a parallel pool (if not already running)
if isempty(gcp('nocreate'))
    parpool('local', 8);
else
    disp('A parallel pool is already open.');
end

addpath(genpath("C:\GitHub\mesoscope"))

%% Load TLCamera DotNet assembly.
ThorFolder = 'C:\Thorlabs\Scientific_Camera_Interfaces_Windows-2.1\Scientific Camera Interfaces\MATLAB';
cd(ThorFolder);
NET.addAssembly([ThorFolder, '\Thorlabs.TSI.TLCamera.dll']);
disp('Dot NET assembly loaded.');

tlCameraSDK = Thorlabs.TSI.TLCamera.TLCameraSDK.OpenTLCameraSDK;
serialNumbers = tlCameraSDK.DiscoverAvailableCameras;
disp([num2str(serialNumbers.Count), ' camera was discovered.']);

if (serialNumbers.Count > 0)
    disp('Opening the first camera')
    tlCamera = tlCameraSDK.OpenCamera(serialNumbers.Item(0), false);
    tlCamera.ExposureTime_us = exposure * 1000;
    gainRange = tlCamera.GainRange;
    if (gainRange.Maximum > 0)
        tlCamera.Gain = 0;
    end
    tlCamera.MaximumNumberOfFramesToQueue = 500;
    tlCamera.OperationMode = Thorlabs.TSI.TLCameraInterfaces.OperationMode.SoftwareTriggered;
    tlCamera.FramesPerTrigger_zeroForUnlimited = 0;
    tlCamera.Arm;
    tlCamera.IssueSoftwareTrigger;

    % Create video writer object
    videoFileName = fullfile(outputFolder, 'acquiredVideo.avi');
    v = VideoWriter(videoFileName, 'Grayscale AVI');  % Specify grayscale for 12-bit frames
    open(v);

    figure(1)
    acquiredFrameNumber = 0;
    numberOfFramesToAcquire = 100000;
    for iloop = 1:numberOfFramesToAcquire
        NumberOfQueuedFrames = tlCamera.NumberOfQueuedFrames;
        if (NumberOfQueuedFrames > 0)
            imageFrame = tlCamera.GetPendingFrameOrNull;
            actualFrameNumber = imageFrame.FrameNumber;
            if ~isempty(imageFrame)
                imageData = imageFrame.ImageData.ImageData_monoOrBGR;
                imageHeight = imageFrame.ImageData.Height_pixels;
                imageWidth = imageFrame.ImageData.Width_pixels;
                imageData2D = reshape(uint16(imageData), [imageWidth, imageHeight]);
                figure(1), imagesc(imageData2D'), colormap(gray), colorbar

                % Normalize the 12-bit frame to the 0-1 range
                normalizedFrame = double(imageData2D) / 4095;  % 4095 is the max value for 12-bit data
                % Convert the normalized frame to 8-bit (0-255)
                frame8bit = uint8(normalizedFrame * 255);

                % Write the 8-bit frame to video
                writeVideo(v, frame8bit);

                acquiredFrameNumber = acquiredFrameNumber + 1;
            end
        end
        disp(["Dropped Frames Count:", num2str(actualFrameNumber - acquiredFrameNumber), ' out of ', num2str(actualFrameNumber)])
        delete(imageFrame);
        drawnow;
    end

    % Close the video writer object
    close(v);

    disp('Stopping software triggered image acquisition.');
    tlCamera.Disarm;
    disp('Releasing the camera');
    tlCamera.Dispose;
    delete(tlCamera);
end

delete(serialNumbers);
tlCameraSDK.Dispose;
delete(tlCameraSDK);
