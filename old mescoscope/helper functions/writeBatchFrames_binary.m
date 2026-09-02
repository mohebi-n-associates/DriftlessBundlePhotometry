function writeBatchFrames_binary(batch, filePath)
   parfor i = 1:numel(batch)
        % Write frame to disk
        fileID = fopen(filePath{i}, 'w');
        fwrite(fileID, batch{i}, 'uint16');
        fclose(fileID);
    end
    % fileID = fopen(filePath{1}, 'w');
    % for i = 1:numel(batch)
    %     Write frame to disk
    %     fwrite(fileID, batch{i}, 'uint16');
    % end
    % fclose(fileID);

end