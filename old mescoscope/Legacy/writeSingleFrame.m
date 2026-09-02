% Helper function to write a single frame
function writeSingleFrame(frame, filename)
    t = Tiff(filename, 'w');

    % % Check if data exceeds 12-bit range
    % maxVal = max(frame(:));
    % if maxVal > 4095
    %     % If data is left-aligned in 16 bits, shift it right
    %     frame = bitshift(frame, -4);
    % end
    % 
    % % Ensure data is within 12-bit range (0-4095)
    % frame = min(frame, 4095);
    
    % Set all tags at once for better performance
    tags = struct();
    tags.ImageLength = 612;  % Your actual height
    tags.ImageWidth = 512;   % Your actual width
    tags.RowsPerStrip = 612; % Full height for single strip
    tags.BitsPerSample = 16;
    tags.SamplesPerPixel = 1;
    tags.PlanarConfiguration = Tiff.PlanarConfiguration.Chunky;
    tags.Photometric = Tiff.Photometric.MinIsBlack;
    tags.Compression = Tiff.Compression.Deflate;  % No compression for speed
    
    % Set all tags at once (faster than individual calls)
    t.setTag(tags);
    
    % Write and close
    t.write(frame);
    t.close();
end