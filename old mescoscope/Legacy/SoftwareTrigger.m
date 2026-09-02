clc;clear all;close all;

profile on

%% Set the variables
exposure=20;
outputFolder="E:\imaging\test2";
mkdir(outputFolder)
% Start a parallel pool (if not already running)
% Check if a parallel pool is already open
if isempty(gcp('nocreate'))
    % No parallel pool open, so create a new one with 8 workers
    parpool('local', 8);
else
    disp('A parallel pool is already open.');
end

addpath(genpath("C:\GitHub\mesoscope"))
actualFrameNumber=0;


%% Load TLCamera DotNet assembly.
ThorFolder = 'C:\Thorlabs\Scientific_Camera_Interfaces_Windows-2.1\Scientific Camera Interfaces\MATLAB';
cd(ThorFolder);
NET.addAssembly([ThorFolder, '\Thorlabs.TSI.TLCamera.dll']);
disp('Dot NET assembly loaded.');

tlCameraSDK = Thorlabs.TSI.TLCamera.TLCameraSDK.OpenTLCameraSDK;
% Get serial numbers of connected TLCamera.
serialNumbers = tlCameraSDK.DiscoverAvailableCameras;
disp([num2str(serialNumbers.Count), ' camera was discovered.']);

if (serialNumbers.Count > 0)
    disp('Opening the first camera')
    tlCamera = tlCameraSDK.OpenCamera(serialNumbers.Item(0), false);
    tlCamera.ExposureTime_us = exposure*1000;
    gainRange = tlCamera.GainRange;
    if (gainRange.Maximum > 0)
        tlCamera.Gain = 0;
    end
    tlCamera.MaximumNumberOfFramesToQueue = 500;
    tlCamera.OperationMode = Thorlabs.TSI.TLCameraInterfaces.OperationMode.SoftwareTriggered;
    tlCamera.FramesPerTrigger_zeroForUnlimited = 0;
    tlCamera.Arm;
    tlCamera.IssueSoftwareTrigger;

    figure(1)
    acquiredFrameNumber=0;
    numberOfFramesToAcquire = 1000;
    while actualFrameNumber<numberOfFramesToAcquire

        NumberOfQueuedFrames = tlCamera.NumberOfQueuedFrames;
        if (NumberOfQueuedFrames > 0)

            % Get the pending image frame.
            imageFrame = tlCamera.GetPendingFrameOrNull;
            actualFrameNumber = imageFrame.FrameNumber;
            if ~isempty(imageFrame)
                % For color images, the image data is in BGR format.
                imageData = imageFrame.ImageData.ImageData_monoOrBGR;

                % TODO: custom image processing code goes here
                imageHeight = imageFrame.ImageData.Height_pixels;
                imageWidth = imageFrame.ImageData.Width_pixels;

                imageData2D = reshape(uint16(imageData), [imageWidth, imageHeight]);
                frameFileName = fullfile(outputFolder, sprintf('frame_%06d.bin', actualFrameNumber));
                %imwrite(imageData2D, frameFileName);
                %parfeval(@imwrite, 0, imageData2D, frameFileName, 'tiff');
                %parfeval(@writeSingleFrame,0,imageData2D,frameFileName);
                %parfeval(@writeSingleFrame_binary,0,imageData2D,frameFileName);
                writeSingleFrame_binary(imageData2D,frameFileName);

                % Release the image frame
                %delete(imageFrame);
                if (mod(actualFrameNumber,10)==0)
                    figure(1),imagesc(imageData2D'), colormap(gray), %colorbar
                    drawnow;
                end
                %disp(num2str(mod(actualFrameNumber,50)))


                acquiredFrameNumber=acquiredFrameNumber+1;
                disp(["Dropped Frames Count:",num2str(actualFrameNumber-acquiredFrameNumber),' out of ',num2str(actualFrameNumber)])
            end


          
        end
    end

    % Stop software triggered image acquisition
    disp('Stopping software triggered image acquisition.');
    tlCamera.Disarm;

    % Release the camera
    disp('Releasing the camera');
    tlCamera.Dispose;
    delete(tlCamera);

end

% Release the serial numbers
delete(serialNumbers);

% Release the TLCameraSDK.
tlCameraSDK.Dispose;
delete(tlCameraSDK);

profile viewer
%profsave