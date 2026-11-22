--[[ TetherInvert hot folder: adds the scans TetherInvert writes to the catalog, where they are.

TetherInvert names folders in ~/.tetherinvert/lightroom/request.txt (a roll's scans/ and prints/);
this plug-in watches them and adds every new TIFF in place. Nothing is moved, copied or
renamed. The TetherInvert metadata in each file is shown in fields of the plug-in's own. ]]

return {
  LrSdkVersion = 6.0,
  LrSdkMinimumVersion = 5.0,
  LrToolkitIdentifier = 'org.tetherinvert.hotfolder',
  LrPluginName = 'TetherInvert hot folder',
  LrForceInitPlugin = true, -- start watching for requests when Lightroom starts, not on first use
  LrInitPlugin = 'Init.lua',
  LrShutdownPlugin = 'Shutdown.lua',
  LrMetadataProvider = 'Metadata.lua', -- TetherInvert Film and TetherInvert Scan, read from the files
  LrMetadataTagsetFactory = 'Tagset.lua',
  LrLibraryMenuItems = {
    { title = 'TetherInvert: watch a folder...', file = 'WatchFolder.lua' },
    { title = 'TetherInvert: stop watching', file = 'StopWatching.lua' },
    { title = 'TetherInvert: status', file = 'ShowStatus.lua' },
    { title = 'TetherInvert: read metadata', file = 'ReadMetadata.lua' },
  },
  LrExportMenuItems = {
    { title = 'TetherInvert: watch a folder...', file = 'WatchFolder.lua' },
    { title = 'TetherInvert: stop watching', file = 'StopWatching.lua' },
    { title = 'TetherInvert: status', file = 'ShowStatus.lua' },
    { title = 'TetherInvert: read metadata', file = 'ReadMetadata.lua' },
  },
  VERSION = { major = 0, minor = 4, revision = 0 },
}
