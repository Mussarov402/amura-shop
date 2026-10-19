"""Товар с фото каталога → PNG без фона для карточки (без ИИ).

Маска: всё заметно не белое → утолщаем → заливаем дырки (белый флакон внутри своего контура) →
оставляем только высокие пятна (товар), а короткие (надписи, плашки «9 гр», водяные знаки) выкидываем.
Если чисто не вышло (товар слился с фоном), make.py ставит фото целиком в светлую рамку."""
import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage


def cut(src, dst, crop=None, thr=236, min_h=.3):
    im = Image.open(src).convert('RGB')
    if crop: im = im.crop(tuple(crop))
    a = np.asarray(im).astype(int)
    ink = (a.min(2) < thr) | ((a.max(2) - a.min(2)) > 22)          # тёмное или цветное
    grown = ndimage.binary_dilation(ink, iterations=5)
    lab, n = ndimage.label(grown)
    keep = np.zeros_like(ink)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if (sl[0].stop - sl[0].start) >= min_h * a.shape[0]: keep |= lab == i
    if not keep.any(): return None
    keep = ndimage.binary_fill_holes(keep)
    keep = ndimage.binary_erosion(keep, iterations=4)
    cover = keep.mean()
    alpha = Image.fromarray((keep * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    out = im.convert('RGBA'); out.putalpha(alpha); box = out.getbbox(); out = out.crop(box)
    s = 1400 / max(out.size)
    if s > 1: out = out.resize((round(out.width * s), round(out.height * s)), Image.LANCZOS).filter(ImageFilter.UnsharpMask(2, 60, 2))
    out.save(dst)
    # «чисто» = товар не занимает почти всё фото (иначе фон не отделился)
    return {'ok': cover < .6, 'cover': round(float(cover), 2), 'size': out.size}
