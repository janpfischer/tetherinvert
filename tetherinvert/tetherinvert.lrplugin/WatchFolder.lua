-- Menu command: choose the folder by hand. Leaves the same request TetherInvert's button does.
local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'
local Watcher = require 'Watcher'

LrTasks.startAsyncTask(function()
  local chosen = LrDialogs.runOpenPanel({
    title = 'Folder TetherInvert writes its TIFFs to',
    prompt = 'Watch',
    canChooseFiles = false,
    canChooseDirectories = true,
    canCreateDirectories = false,
    allowsMultipleSelection = false,
  })
  if chosen and chosen[1] then
    Watcher.writeRequest(chosen[1])
  end
end)
