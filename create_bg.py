#!/usr/bin/env python3

import sys
from pathlib import Path
from PIL import Image, ImageOps

WIDTH = 622
HEIGHT = 345
CF = 9  # LV_IMG_CF_INDEXED_4BIT


def convert(source, destination):
    img = Image.open(source).convert("RGB")

    # Ajusta para 622x345 preenchendo toda a tela.
    # Corta as bordas se a proporção for diferente.
    img = ImageOps.fit(
        img,
        (WIDTH, HEIGHT),
        method=Image.Resampling.LANCZOS
    )

    # Reduz para no máximo 16 cores.
    img = img.quantize(
        colors=16,
        method=Image.Quantize.MEDIANCUT
    )

    palette = img.getpalette()

    # Header LVGL v8
    header = (
        CF |
        (WIDTH << 10) |
        (HEIGHT << 21)
    )

    output = bytearray()
    output += header.to_bytes(4, "little")

    # Paleta LVGL: BGRA
    for i in range(16):
        r = palette[i * 3 + 0]
        g = palette[i * 3 + 1]
        b = palette[i * 3 + 2]

        output += bytes([
            b,
            g,
            r,
            255
        ])

    pixels = list(img.getdata())

    # 2 pixels por byte:
    #
    # pixel A = nibble alto
    # pixel B = nibble baixo
    for i in range(0, len(pixels), 2):
        a = pixels[i] & 0x0F

        if i + 1 < len(pixels):
            b = pixels[i + 1] & 0x0F
        else:
            b = 0

        output.append((a << 4) | b)

    Path(destination).write_bytes(output)

    print(f"Criado: {destination}")
    print(f"Resolução: {WIDTH}x{HEIGHT}")
    print(f"Tamanho: {len(output)} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Uso: {sys.argv[0]} imagem.png bg.bin")
        sys.exit(1)

    convert(sys.argv[1], sys.argv[2])
