Pod::Spec.new do |s|
  s.name           = 'SwingTrim'
  s.version        = '1.0.0'
  s.summary        = 'Lossless swing trimming and a motion profile for choosing the default trim'
  s.description    = s.summary
  s.license        = 'MIT'
  s.author         = 'Swing Guru'
  s.homepage       = 'https://swingguru.app'
  s.platforms      = { :ios => '16.4' }
  s.swift_version  = '5.9'
  s.source         = { git: '' }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'

  s.source_files = "**/*.{h,m,swift}"
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES'
  }
end
