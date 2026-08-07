$fn = 96;

// G-Shock-on-Crocs prototype v2.
// This is a real Jibbitz-style shoe plug with a compact snap cradle, not the
// desk stand that was mistakenly shipped as v1. Dimensions are millimetres.

case_width = 44.5;       // DW-5600 / GA-2100 class; flex walls open ~2 mm
case_length = 50;
platform_t = 3.6;
wall_t = 2.4;
wall_h = 11.5;
lip = 1.3;

module rounded_box(size=[10,10,10], r=2) {
  hull()
    for (x=[-size[0]/2+r,size[0]/2-r])
      for (y=[-size[1]/2+r,size[1]/2-r])
        translate([x,y,0]) cylinder(h=size[2], r=r);
}

module peg() {
  // Rounded 12.8 mm retaining button, 7.6 mm neck and 20 mm top load pad.
  cylinder(h=0.6, r1=5.5, r2=6.4);
  translate([0,0,0.5]) cylinder(h=1.7, r=6.4);
  translate([0,0,2.1]) cylinder(h=3.9, r=3.8);
  // 0.2 mm overlap with the neck avoids a merely coplanar/non-fused seam.
  translate([0,0,5.8]) cylinder(h=2.9, r1=8.2, r2=10);
}

union() {
  peg();

  // Low-profile platform distributes the watch load over the shoe.
  translate([0,0,8.4])
    rounded_box([case_width+7.2, case_length+7, platform_t], 3);

  // Flexible side rails snap around the watch case. Front/back remain open so
  // the original G-Shock straps can exit naturally along the shoe.
  for (sx=[-1,1]) {
    translate([sx*(case_width/2+wall_t/2),0,11.6])
      rounded_box([wall_t,case_length,wall_h], 1.0);
    translate([sx*(case_width/2+(wall_t-lip)/2),
               0,22.1])
      rounded_box([wall_t+lip,case_length-5,1.8], 0.7);
  }

  // Four corner stops prevent case slide while the original 28 mm-or-narrower
  // watch straps exit through the open center of each end.
  for (sy=[-1,1], sx=[-1,1])
    translate([sx*18.25,sy*(case_length/2+wall_t/2),11.6])
      rounded_box([8,wall_t,5.5], 0.8);
}
