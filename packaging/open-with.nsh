!define ANVIL_CLASS "AnvilIDE.Source"
!define ANVIL_APPS "Software\Classes\Applications\${PRODUCT_FILENAME}.exe"
!define ANVIL_CAP "Software\AnvilIDE\Capabilities"

!macro SourceTypes operation
  !insertmacro ${operation} ".c"
  !insertmacro ${operation} ".cc"
  !insertmacro ${operation} ".cpp"
  !insertmacro ${operation} ".cxx"
  !insertmacro ${operation} ".h"
  !insertmacro ${operation} ".hh"
  !insertmacro ${operation} ".hpp"
  !insertmacro ${operation} ".hxx"
  !insertmacro ${operation} ".py"
  !insertmacro ${operation} ".java"
  !insertmacro ${operation} ".kt"
  !insertmacro ${operation} ".kts"
  !insertmacro ${operation} ".js"
  !insertmacro ${operation} ".mjs"
  !insertmacro ${operation} ".cjs"
  !insertmacro ${operation} ".jsx"
  !insertmacro ${operation} ".ts"
  !insertmacro ${operation} ".mts"
  !insertmacro ${operation} ".cts"
  !insertmacro ${operation} ".tsx"
  !insertmacro ${operation} ".rs"
  !insertmacro ${operation} ".go"
  !insertmacro ${operation} ".css"
  !insertmacro ${operation} ".html"
  !insertmacro ${operation} ".json"
  !insertmacro ${operation} ".md"
  !insertmacro ${operation} ".txt"
  !insertmacro ${operation} ".yaml"
  !insertmacro ${operation} ".yml"
  !insertmacro ${operation} ".toml"
!macroend

!macro RegisterSource ext
  WriteRegStr HKCU "Software\Classes\${ext}\OpenWithProgids" "${ANVIL_CLASS}" ""
  WriteRegStr HKCU "${ANVIL_APPS}\SupportedTypes" "${ext}" ""
  WriteRegStr HKCU "${ANVIL_CAP}\FileAssociations" "${ext}" "${ANVIL_CLASS}"
!macroend

!macro UnregisterSource ext
  DeleteRegValue HKCU "Software\Classes\${ext}\OpenWithProgids" "${ANVIL_CLASS}"
  DeleteRegKey /ifempty HKCU "Software\Classes\${ext}\OpenWithProgids"
  DeleteRegKey /ifempty HKCU "Software\Classes\${ext}"
!macroend

!macro customInstall
  WriteRegStr HKCU "Software\Classes\${ANVIL_CLASS}" "" "Anvil IDE source file"
  WriteRegStr HKCU "Software\Classes\${ANVIL_CLASS}\Application" "ApplicationName" "Anvil IDE"
  WriteRegStr HKCU "Software\Classes\${ANVIL_CLASS}\DefaultIcon" "" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}",0'
  WriteRegStr HKCU "Software\Classes\${ANVIL_CLASS}\shell\open\command" "" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" -- "%1"'
  WriteRegStr HKCU "${ANVIL_APPS}" "FriendlyAppName" "Anvil IDE"
  WriteRegStr HKCU "${ANVIL_APPS}\shell\open\command" "" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" -- "%1"'
  WriteRegStr HKCU "${ANVIL_CAP}" "ApplicationName" "Anvil IDE"
  WriteRegStr HKCU "${ANVIL_CAP}" "ApplicationDescription" "Anvil IDE source code editor"
  WriteRegStr HKCU "${ANVIL_CAP}" "ApplicationIcon" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}",0'
  WriteRegStr HKCU "Software\RegisteredApplications" "Anvil IDE" "${ANVIL_CAP}"
  !insertmacro SourceTypes RegisterSource
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend

!macro customUnInstall
  !insertmacro SourceTypes UnregisterSource
  DeleteRegKey HKCU "Software\Classes\${ANVIL_CLASS}"
  DeleteRegKey HKCU "${ANVIL_APPS}"
  DeleteRegValue HKCU "Software\RegisteredApplications" "Anvil IDE"
  DeleteRegKey HKCU "${ANVIL_CAP}"
  DeleteRegKey /ifempty HKCU "Software\AnvilIDE"
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend
