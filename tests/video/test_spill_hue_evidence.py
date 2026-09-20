"""Spill decisions distinguish key hue from warm material and edge contamination."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sprite_gen.video import frames


def _reference(path: Path, key: str, color: tuple[int, int, int], *, fringe=False):
    bg = (0, 255, 0) if key == 'green' else (255, 0, 255)
    im = Image.new('RGB', (160, 160), bg)
    draw = ImageDraw.Draw(im)
    draw.rectangle((24, 24, 135, 135), fill=color)
    if fringe:
        draw.rectangle((24, 24, 135, 135), outline=(4, 19, 6), width=2)
    im.save(path)
    return path


@pytest.mark.parametrize('key,color', [
    ('green', (235, 210, 65)),   # yellow: green exceeds the mean, but not red
    ('green', (65, 210, 235)),   # cyan: green exceeds the mean, but not blue
    ('magenta', (220, 80, 65)),  # red: red/blue mean is not evidence of magenta
    ('magenta', (65, 80, 220)),  # blue
])
def test_non_key_hues_do_not_disable_full_or_get_recoloured(tmp_path, key, color):
    source = _reference(tmp_path/'ref.png', key, color)
    assert frames.decide_spill(source, key)['mode'] == 'full'
    frames.key_frames([source], tmp_path/'out', key=key, check_edges=False, spill='full')
    assert Image.open(tmp_path/'out/ref.png').getpixel((80, 80)) == (*color, 255)


def test_a_dark_key_tinted_outline_is_not_interior_material(tmp_path):
    source = _reference(tmp_path/'ref.png', 'green', (220, 220, 220), fringe=True)
    decision = frames.decide_spill(source, 'green')
    assert decision['mode'] == 'full'
    assert decision['key_material_px'] == 0


@pytest.mark.parametrize('key,color', [('green',(80,115,90)),('magenta',(115,80,110))])
def test_genuine_mild_key_material_still_keeps_conservative_mode(tmp_path,key,color):
    source = _reference(tmp_path/'ref.png',key,color)
    assert frames.decide_spill(source,key)['mode']=='small'


@pytest.mark.parametrize('key,color', [('green',(180,220,195)),('magenta',(220,180,210))])
def test_full_removes_key_excess_without_punching_alpha(tmp_path,key,color):
    source=_reference(tmp_path/'ref.png',key,color)
    frames.key_frames([source],tmp_path/'full',key=key,check_edges=False,spill='full')
    frames.key_frames([source],tmp_path/'small',key=key,check_edges=False,spill='small')
    full=Image.open(tmp_path/'full/ref.png');small=Image.open(tmp_path/'small/ref.png')
    assert full.getchannel('A').tobytes()==small.getchannel('A').tobytes()
    r,g,b,a=full.getpixel((80,80))
    assert a==255
    assert (g-max(r,b) if key=='green' else min(r,b)-g)<=1


def test_a_bright_thin_green_detail_at_the_outline_is_material(tmp_path):
    source = _reference(tmp_path/'ref.png', 'green', (220, 220, 220))
    im = Image.open(source)
    # A mild green survives the unchanged edge matte; stronger boundary greens
    # are already unmixed in the baseline, before the spill decision runs.
    ImageDraw.Draw(im).rectangle((24, 24, 135, 135), outline=(160, 180, 165), width=2)
    im.save(source)
    assert frames.decide_spill(source, 'green')['mode'] == 'small'
