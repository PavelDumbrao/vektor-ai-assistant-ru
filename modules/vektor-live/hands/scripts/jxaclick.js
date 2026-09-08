// Реальный клик мышью по экранным координатам (CoreGraphics): move → down → up с clickState=1 и HID-источником.
ObjC.import('CoreGraphics');
function run(argv){
  var x=parseFloat(argv[0]), y=parseFloat(argv[1]); var pt={x:x,y:y};
  var src=$.CGEventSourceCreate($.kCGEventSourceStateHIDSystemState);
  var mv=$.CGEventCreateMouseEvent(src,$.kCGEventMouseMoved,pt,$.kCGMouseButtonLeft); $.CGEventPost($.kCGHIDEventTap,mv); delay(0.15);
  var dn=$.CGEventCreateMouseEvent(src,$.kCGEventLeftMouseDown,pt,$.kCGMouseButtonLeft); $.CGEventSetIntegerValueField(dn,$.kCGMouseEventClickState,1); $.CGEventPost($.kCGHIDEventTap,dn); delay(0.08);
  var up=$.CGEventCreateMouseEvent(src,$.kCGEventLeftMouseUp,pt,$.kCGMouseButtonLeft); $.CGEventSetIntegerValueField(up,$.kCGMouseEventClickState,1); $.CGEventPost($.kCGHIDEventTap,up);
  return "clicked "+x+","+y;
}
