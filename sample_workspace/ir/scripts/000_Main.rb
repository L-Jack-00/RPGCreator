# Cleanroom RGSS entry script
begin
  Graphics.freeze
  $scene = Scene_Title.new
  $scene.main while $scene != nil
  Graphics.transition(20)
rescue Errno::ENOENT
  filename = $!.message.sub('No such file or directory - ', '')
  print("Unable to find file #{filename}.")
end
