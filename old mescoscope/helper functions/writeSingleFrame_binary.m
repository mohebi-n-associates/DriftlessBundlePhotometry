% Helper function to write a single frame
function writeSingleFrame_binary(frame, filename)

% Open the file for writing in binary mode
fileID = fopen(filename, 'w');

% Write the image data to the file
fwrite(fileID, frame, 'uint16');

% Close the file
fclose(fileID);


end