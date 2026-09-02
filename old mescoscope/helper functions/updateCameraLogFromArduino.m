function updateCameraLogFromArduino(serialPortName, logFileName, mode,LEDonDuration,LEDonBuffer)
    serialPort = serialport(serialPortName, 115200); % Adjust as needed
    configureTerminator(serialPort, "LF");
    %fopen(serialPort);
    logFileID = fopen(logFileName, 'a'); % Append to log file

    params = sprintf('%G+', mode,LEDonDuration,LEDonBuffer);
    params = params(1:end-1);
    pause(2)
    writeline(serialPort, params); % Send info to Arduino
    flush(serialPort);

    while true
        %pause(0.002)
        if serialPort.BytesAvailable > 0 % Check if data is available to read
            arduinoData = readline(serialPort); % Read available data
            input_string = strtrim(arduinoData); % Strip leading/trailing spaces and newlines
            split_string = strsplit(input_string, ',');
            fprintf(logFileID, '%s,', strtrim(split_string{1}));
            fprintf(logFileID, '%s,', strtrim(split_string{2}));
            fprintf(logFileID, '%s\n', strtrim(split_string{3}));
        end
    end
end