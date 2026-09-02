% Initialize webcam
cam = webcam;

% Create a parallel pool if it doesn't exist
if isempty(gcp('nocreate'))
    parpool;
end

% Directory to save frames
outputDir = 'webcam_frames';
if ~exist(outputDir, 'dir')
    mkdir(outputDir);
end

% Frame counter
frameCount = 0;
futures = parallel.FevalFuture.empty;

% Main acquisition loop
while true
    % Acquire frame
    frame = snapshot(cam);
    frameCount = frameCount + 1;
    
    % Generate filename
    filename = fullfile(outputDir, sprintf('frame_%06d.bin', frameCount));
    
    % Save frame in the background
    futures(end+1) = parfeval(@saveFrameAsBinary, 0, frame, filename);
    
    % Periodically check and clear completed futures
    if mod(frameCount, 100) == 0
        % Check the state of each future
        for i = numel(futures):-1:1
            if strcmp(futures(i).State, 'finished')
                % Delete the completed future
                delete(futures(i));
                futures(i) = [];
            end
        end
    end
    
    % Optionally, add a condition to break the loop
    if frameCount >= 10000
        break;
    end
end

% Wait for all futures to complete
wait(futures);

% Clean up
clear cam;

% Function to save frame as binary file
function saveFrameAsBinary(frame, filename)
    % Open file for writing
    fileID = fopen(filename, 'w');
    % Write frame data as binary
    fwrite(fileID, frame, 'uint8');
    % Close file
    fclose(fileID);
end