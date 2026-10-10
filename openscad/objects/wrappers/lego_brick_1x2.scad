// orcad wrapper for cfinke/LEGO.scad (MIT): block().
use <lego-scad-d717ca8e/LEGO.scad>

brick_width = 1;
brick_length = 2;
brick_height = 1;
brick_scale = 1;
with_posts = true;

block(width = brick_width,
      length = brick_length,
      height = brick_height,
      type = "brick",
      stud_type = "solid",
      brand = "lego",
      block_bottom_type = "open",
      scale = brick_scale,
      with_posts = with_posts);
