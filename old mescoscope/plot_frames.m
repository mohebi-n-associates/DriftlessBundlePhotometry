% Choose folder containing .bin files
folderPath = uigetdir(pwd, 'Select folder containing .bin files');

% List all .bin files in the folder
binFiles = dir(fullfile(folderPath, '*.bin'));

% Initialize vector to hold average values
avgValues = zeros(length(binFiles), 1);

% Loop through each file
for k = 1:length(binFiles)
    % Full path to the file
    filePath = fullfile(folderPath, binFiles(k).name);

    % Open file
    fid = fopen(filePath, 'r');
    if fid == -1
        warning('Could not open file: %s', filePath);
        continue;
    end

    % Read as uint16
    data = fread(fid, inf, 'uint16');
    fclose(fid);

    % Compute average and store
    avgValues(k) = mean(double(data));
end

% Plot results
figure;
plot(avgValues);
xlabel('File Index');
ylabel('Average Intensity (uint16)');
title('Mean Value of Each .bin File');
grid on;
