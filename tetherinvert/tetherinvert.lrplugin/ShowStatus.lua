-- Menu command: what the plug-in is doing right now.
local LrDialogs = import 'LrDialogs'
local Watcher = require 'Watcher'

local state = Watcher.state
local headline
if not state.running then
  headline = 'The TetherInvert hot folder is not running. Reload the plug-in in the Plug-in Manager.'
elseif state.folders then
  headline = 'Watching ' .. table.concat(state.folders, ' and ')
else
  headline = 'Not watching a folder. Press "Open in Lightroom Classic" in TetherInvert, or choose a folder here.'
end
local details = 'Added since the watch began: ' .. tostring(state.added or 0)
  .. '\nCould not be added: ' .. tostring(state.failed or 0)
  .. '\nPlug-in version: ' .. Watcher.VERSION
  .. '\nLog: ' .. Watcher.logFile
LrDialogs.message(headline, details, 'info')
