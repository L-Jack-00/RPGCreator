MRuby::Build.new do |conf|
  conf.toolchain :visualcpp
  conf.cc.defines << "MRB_UTF8_STRING"

  # Core language + RGSS1/RGSS3 compatible standard gems (MIT core, mruby 4.0)
  conf.gembox "stdlib"
  conf.gembox "stdlib-ext"
  conf.gembox "math"
  conf.gembox "metaprog"

  conf.gem core: 'mruby-eval'
  conf.gem core: 'mruby-error'
  conf.gem core: 'mruby-encoding'
  conf.gem core: 'mruby-exit'
  conf.gem core: 'mruby-bigint'
  conf.gem core: 'mruby-rational'
  conf.gem core: 'mruby-complex'
  conf.gem core: 'mruby-errno'
  conf.gem core: 'hal-win-io'
  conf.gem core: 'mruby-io'
  conf.gem core: 'hal-win-dir'
  conf.gem core: 'mruby-dir'
end
