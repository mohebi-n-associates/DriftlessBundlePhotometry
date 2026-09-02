

mode=7;

% Set the serial port name and log file name
serialPortName = 'COM3'; % Adjust as needed
logFileName = 'cameraLog_ali_gambaloo.txt';


if isempty(gcp('nocreate'))
    parpool;
end

% Use parfeval to run the function in the background
f = parfeval(@updateCameraLogFromArduino, 0, serialPortName, logFileName, mode);


