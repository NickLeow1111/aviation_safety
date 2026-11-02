/**
 * The airliner silhouette, drawn once and shared.
 *
 * Both the live map and the replay pane need the same shape pointing north, so a symbol
 * layer can rotate it by the reported track. Canvas rather than a sprite sheet: no extra
 * asset to ship, and the colour variants stay in one place.
 */
export function planeImage(colour: string, outline = "#ffffff"): ImageData {
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2d canvas unavailable");

  ctx.beginPath();
  ctx.moveTo(24, 3);                                   // nose
  ctx.bezierCurveTo(26.4, 8, 27.6, 13, 27.6, 19);      // fuselage, right
  ctx.lineTo(45, 31);                                  // right wing leading edge
  ctx.lineTo(45, 35);
  ctx.lineTo(27.6, 28);                                // wing trailing edge
  ctx.lineTo(27.6, 38);
  ctx.lineTo(34, 42);                                  // right tailplane
  ctx.lineTo(34, 45);
  ctx.lineTo(24, 42.5);
  ctx.lineTo(14, 45);                                  // left tailplane
  ctx.lineTo(14, 42);
  ctx.lineTo(20.4, 38);
  ctx.lineTo(20.4, 28);
  ctx.lineTo(3, 35);                                   // left wing
  ctx.lineTo(3, 31);
  ctx.lineTo(20.4, 19);
  ctx.bezierCurveTo(20.4, 13, 21.6, 8, 24, 3);         // fuselage, left
  ctx.closePath();

  ctx.fillStyle = colour;
  ctx.strokeStyle = outline;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fill();
  return ctx.getImageData(0, 0, size, size);
}
