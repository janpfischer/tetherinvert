-- Menu command: the TetherInvert fields of the selected photos, read from their files, however and
-- whenever they were imported. With one photo selected or none, every photo shown: clicking a
-- folder leaves one selected, and the folder is meant. Photos without TetherInvert metadata are
-- skipped and counted.
local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'
local Watcher = require 'Watcher'

LrTasks.startAsyncTask(function()
  local photos = LrApplication.activeCatalog():getMultipleSelectedOrAllPhotos()
  local done = Watcher.readPhotos(photos)
  if done.busy then
    LrDialogs.message('TetherInvert: the catalog is busy', 'Try again in a moment.', 'warning')
    return
  end
  LrDialogs.message(
    'TetherInvert: read metadata of ' .. tostring(#photos) .. ' photo' .. (#photos == 1 and '' or 's'),
    'Updated: ' .. tostring(done.updated)
      .. '\nAlready up to date: ' .. tostring(done.current)
      .. '\nWithout TetherInvert metadata, skipped: ' .. tostring(done.skipped)
      .. '\n\nCamera, lens, exposure and capture time: Metadata > Read Metadata from Files.',
    'info'
  )
end)
