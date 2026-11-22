-- Runs when the plug-in is disabled or reloaded: ends the loop the init script started.
if tetherinvertHotFolder then
  tetherinvertHotFolder.running = false
end
