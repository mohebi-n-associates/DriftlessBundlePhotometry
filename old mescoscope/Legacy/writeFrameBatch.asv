function writeFrameBatch(frameBatch, channelFolder)
    % Writes a batch of frames to disk
    for i = 1:numel(frameBatch)
        frameStruct = frameBatch{i};
        if ~isempty(frameStru ...
                ct)
            filename = sprintf('image_%06d.tif', frameStruct.FrameNumber);
            filePath = fullfile(channelFolder, filename);
            imwrite(frameStruct.Image, filePath, 'tif');
        end
    end
end