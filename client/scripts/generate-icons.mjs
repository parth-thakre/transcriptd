import { deflateSync } from 'node:zlib'
import { writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

function crc32(bytes) {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0)
  }
  return (crc ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const tag = Buffer.from(type), size = Buffer.alloc(4), crc = Buffer.alloc(4)
  size.writeUInt32BE(data.length); crc.writeUInt32BE(crc32(Buffer.concat([tag, data])))
  return Buffer.concat([size, tag, data, crc])
}
function png(size) {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6
  const pixels = Buffer.alloc(size * (size * 4 + 1))
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const px = (x + .5) / size, py = (y + .5) / size
    const circle = (px - .5) ** 2 + (py - .5) ** 2 < .235
    const mic = Math.abs(px - .5) < .09 && py > .2 && py < .56
    const cup = py > .4 && py < .67 && Math.abs(px - .5) < .21 && (py > .59 || Math.abs(px - .5) > .16)
    const stand = (Math.abs(px - .5) < .025 && py > .64 && py < .79) || (Math.abs(px - .5) < .13 && py > .76 && py < .8)
    const i = y * (size * 4 + 1) + 1 + x * 4
    pixels.set(mic || cup || stand ? [235, 250, 245, 255] : [25, 66, 57, circle ? 255 : 0], i)
  }
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))])
}
for (const [name, size] of [['tray.png', 32], ['icon.png', 512]]) {
  writeFileSync(fileURLToPath(new URL(`../resources/${name}`, import.meta.url)), png(size))
}
