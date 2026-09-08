-- Поиск элементов по видимой подписи. Строки: роль|подпись|центр X|центр Y|ширина|высота
-- Обход в глубину с ограничением: "entire contents" на больших приложениях падает или виснет.
property matches : {}
property target : ""
property maxDepth : 6
property maxHits : 10

on labelOf(e)
  tell application "System Events"
    set nm to ""
    try
      set nm to (name of e) as text
    end try
    if nm is "" or nm is "missing value" then
      try
        set nm to (description of e) as text
      end try
    end if
    if nm is "" or nm is "missing value" then
      try
        set nm to (title of e) as text
      end try
    end if
    if nm is "" or nm is "missing value" then
      try
        set v to (value of e)
        if class of v is text then set nm to v
      end try
    end if
  end tell
  if nm is "missing value" then set nm to ""
  return nm
end labelOf

on walk(el, depth)
  if depth > maxDepth then return
  if (count of matches) ≥ maxHits then return
  tell application "System Events"
    set kids to {}
    try
      set kids to UI elements of el
    end try
    repeat with k in kids
      if (count of my matches) ≥ maxHits then return
      set nm to my labelOf(k)
      if nm is not "" and nm contains my target then
        try
          set p to position of k
          set s to size of k
          if (item 1 of s) > 0 and (item 2 of s) > 0 then
            set cx to (item 1 of p) + ((item 1 of s) div 2)
            set cy to (item 2 of p) + ((item 2 of s) div 2)
            set end of my matches to ((role of k) as text) & "|" & nm & "|" & cx & "|" & cy & "|" & (item 1 of s) & "|" & (item 2 of s)
          end if
        end try
      end if
      my walk(k, depth + 1)
    end repeat
  end tell
end walk

on run argv
  set target to item 1 of argv
  set matches to {}
  set procName to ""
  if (count of argv) > 1 then set procName to item 2 of argv
  tell application "System Events"
    if procName is "" then
      set p to first application process whose frontmost is true
    else
      set p to application process procName
    end if
    set appName to name of p
    set wins to {}
    try
      set wins to windows of p
    end try
  end tell
  if (count of wins) = 0 then return "APP:" & appName & linefeed & "NOWINDOW"
  repeat with w in wins
    my walk(w, 1)
    if (count of matches) ≥ maxHits then exit repeat
  end repeat
  set AppleScript's text item delimiters to linefeed
  return "APP:" & appName & linefeed & (matches as text)
end run
