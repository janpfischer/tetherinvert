-- The Metadata panel's "TetherInvert" set: the plug-in's fields under their two headings.
local P = 'org.tetherinvert.hotfolder.'

return {
  id = 'tetherinvertTagset',
  title = 'TetherInvert',
  items = {
    'com.adobe.filename',
    'com.adobe.caption',
    'com.adobe.separator',
    { 'com.adobe.label', label = 'TetherInvert Film' },
    P .. 'filmStock', P .. 'filmFormat', P .. 'boxSpeed', P .. 'exposureIndex', P .. 'developer', P .. 'dateDeveloped', P .. 'dateShotTo',
    'com.adobe.separator',
    { 'com.adobe.label', label = 'TetherInvert Scan' },
    P .. 'scanRoll', P .. 'scanFrame', P .. 'scanFile', P .. 'scanCamera', P .. 'scanLight', P .. 'scanPreset',
    P .. 'scanExposure', P .. 'scanFlatField', P .. 'scanTime', P .. 'scanBase', P .. 'scanCorrections', P .. 'scanSoftware',
  },
}
