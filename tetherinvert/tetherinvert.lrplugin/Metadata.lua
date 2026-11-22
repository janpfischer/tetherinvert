--[[ The plug-in's own metadata fields: TetherInvert Film and TetherInvert Scan, read from the XMP
TetherInvert writes into every TIFF (Watcher.FIELDS says where each one is in the file). Read only:
TetherInvert is where they are entered. The film fields can filter the Library. ]]

return {
  metadataFieldsForPhotos = {
    { id = 'filmStock', title = 'Film stock', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'filmFormat', title = 'Format', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'boxSpeed', title = 'Box speed', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'exposureIndex', title = 'Shot at EI', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'developer', title = 'Developer or lab', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'dateDeveloped', title = 'Date developed', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'dateShotTo', title = 'Shot until', dataType = 'string', readOnly = true, searchable = true, browsable = true },
    { id = 'scanRoll', title = 'Roll', dataType = 'string', readOnly = true, searchable = true },
    { id = 'scanFrame', title = 'Frame', dataType = 'string', readOnly = true },
    { id = 'scanFile', title = 'File', dataType = 'string', readOnly = true },
    { id = 'scanCamera', title = 'Scanned with', dataType = 'string', readOnly = true },
    { id = 'scanLight', title = 'Light', dataType = 'string', readOnly = true },
    { id = 'scanPreset', title = 'Preset', dataType = 'string', readOnly = true },
    { id = 'scanExposure', title = 'Exposure', dataType = 'string', readOnly = true },
    { id = 'scanFlatField', title = 'Flat field', dataType = 'string', readOnly = true },
    { id = 'scanTime', title = 'Scanned', dataType = 'string', readOnly = true },
    { id = 'scanBase', title = 'Base', dataType = 'string', readOnly = true },
    { id = 'scanCorrections', title = 'Corrections', dataType = 'string', readOnly = true },
    { id = 'scanSoftware', title = 'Software', dataType = 'string', readOnly = true },
    -- Not from a TetherInvert namespace: the keywords the plug-in set, so it removes only those of
    -- its own that TetherInvert dropped since, never one typed in Lightroom.
    { id = 'keywordsSet', title = 'Keywords from TetherInvert', dataType = 'string', readOnly = true },
  },
  schemaVersion = 3, -- raise when a field is added or changed
}
