# iPhone brightness at the alarm

A web page cannot set the iPhone screen brightness. tinyLight uses two ways around it.

1. HDR glow (automatic). While the alarm rings, the phone page plays a small HDR warm-white clip
   behind the Stop button. iOS raises the panel brightness for HDR content. It works only while the
   page is open in night mode. It does not move the system brightness slider.
2. Shortcuts automation (one-time setup, full system brightness):
   1. Open Shortcuts → Automation → New Automation → Time of Day.
   2. Set the alarm time (for a 30 min sunrise, the alarm time minus 30 min) and the days.
   3. Choose Run Immediately.
   4. Add the action Set Brightness, 100 %.
   5. Optional: a second automation, Alarm → Is Stopped, Set Brightness 20 %.
   The automation does not know when you change the alarm in tinyLight; update its time by hand.
