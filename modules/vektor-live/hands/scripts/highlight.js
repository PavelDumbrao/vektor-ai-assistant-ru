// Подсветка точки на экране: безрамочное прозрачное окно поверх всего, красное кольцо.
// Курсор и фокус не трогаются — Павел продолжает работать, просто видит, куда нажать.
// Использование: osascript -l JavaScript highlight.js X Y [секунды] [подпись]
ObjC.import('Cocoa');
ObjC.import('QuartzCore');

function run(argv) {
  const x = parseFloat(argv[0]);
  const y = parseFloat(argv[1]);            // координаты сверху, как у Accessibility
  const secs = parseFloat(argv[2] || '2.5');
  const label = argv[3] || '';

  // без инициализации приложения NSWindow не создаётся
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);

  const screen = $.NSScreen.screens.objectAtIndex(0);
  const screenH = screen.frame.size.height;
  const R = 46;                              // радиус кольца
  const boxW = R * 2 + (label ? 240 : 0);
  const boxH = R * 2;
  // Cocoa считает от нижнего левого угла — переворачиваем Y
  const rect = $.NSMakeRect(x - R, screenH - y - R, boxW, boxH);

  const win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
    rect, 0 /* borderless */, 2 /* buffered */, false);
  win.setOpaque(false);
  win.setBackgroundColor($.NSColor.clearColor);
  win.setIgnoresMouseEvents(true);           // клики проходят сквозь — работе не мешает
  win.setLevel(2147483631);                  // поверх всех окон
  win.setHasShadow(false);
  win.setCollectionBehavior((1 << 0) | (1 << 6)); // на всех рабочих столах, поверх фуллскрина

  const view = $.NSView.alloc.initWithFrame($.NSMakeRect(0, 0, boxW, boxH));
  view.setWantsLayer(true);

  // кольцо рисуем слоем собственного подвида: CALayer напрямую из JXA не создаётся
  const ringView = $.NSView.alloc.initWithFrame($.NSMakeRect(3, 3, R * 2 - 6, R * 2 - 6));
  ringView.setWantsLayer(true);
  ringView.layer.cornerRadius = R - 3;
  ringView.layer.borderWidth = 4;
  ringView.layer.borderColor = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.95, 0.30, 0.15, 1.0).CGColor;
  ringView.layer.backgroundColor = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.95, 0.30, 0.15, 0.13).CGColor;
  view.addSubview(ringView);

  if (label) {
    const text = $.NSTextField.alloc.initWithFrame($.NSMakeRect(R * 2 + 8, R - 15, boxW - R * 2 - 12, 30));
    text.setStringValue(label);
    text.setBezeled(false);
    text.setDrawsBackground(true);
    text.setBackgroundColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.08, 0.09, 0.11, 0.92));
    text.setTextColor($.NSColor.whiteColor);
    text.setEditable(false);
    text.setSelectable(false);
    text.setFont($.NSFont.boldSystemFontOfSize(15));
    view.addSubview(text);
  }

  win.setContentView(view);
  win.orderFrontRegardless;
  $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(secs));
  win.close;
  return 'highlighted ' + x + ',' + y;
}
