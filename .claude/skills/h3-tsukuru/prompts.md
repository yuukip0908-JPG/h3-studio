# 6秒プロンプト集（Image Trace = 元画像を最初のコマにしてプロンプトで動かす）

書き方は Vera（@VeraVCreates, 2026-09-03）の H3 プロンプトにならう: 動きごとに秒数を書き、
最後に「〜しない」を並べる。型は GIRLWARE（@GirlwareAI）で伸びた動画の共通点（何も起きない
数秒 → 最後に目が合う。2026-09-22〜10-04 の12本で中央値 約5万回、上位 52万 / 31万 / 23万回）から。

- 送るときは `{{SUBJECT}}` を元画像の説明に置き換える（例: `long black hair, the same black ribbed knit dress and the same room`）
- 2026-10-05 の1本目で「A few strands of hair move」が強風になった。室内では必ず「no wind」と書く
- 実測（6秒・0.4MP・12 steps・RTX 6000 Ada）: MISS 284秒 / HIT 155秒

## 共通の頭

```
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description:
[Shot 1] Live-action vertical 9:16 smartphone video posted as an Instagram story. The phone is propped on a shelf, the camera is locked off, soft window light, slight phone-camera softness. A young Korean woman matching <Picture 1> exactly: same face, {{SUBJECT}}, framed from the top of her head to mid-thigh. She stays standing in place the whole time and does not walk. There is no wind; her hair only moves with her head.
```

## 共通の尻

```
Every movement is slow and unhurried. Her clothing never changes. No walking, no cuts, no zoom, no camera movement, no spinning, hands never cover the face, no props, no extra fingers, no plastic skin.

overall_soundscape: quiet room tone, faint street noise outside the window, soft fabric rustle.

non_diegetic_music: N/A
```

## A. 気づいて目が合う

```
She is looking out of the window on her right, her face in three-quarter view, her expression calm and a little absent. She blinks slowly once. At about 00:03.5 she notices the camera: only her eyes move to the lens first, then her head follows, taking about 0.8 seconds. She holds the look for a moment without expression. Then a small closed-lip smile appears and stays until the end.
```

## B. 服を整えてから見つめ返す

```
She lightly straightens the cuff of her left sleeve with her right hand, then smooths the hem of her dress with both hands, taking about 2 seconds in total. She glances around the room once. At about 00:03.5 she turns her face to the lens and looks straight into it for about 2 seconds without smiling, breathing naturally. In the last half second the corner of her mouth lifts slightly.
```

## C. 横顔から向き直って指ハート

```
Her head is turned to her right, showing her profile, as if listening to someone off-frame. At about 00:01.5 she slowly turns her face back to the lens, taking about 1 second. Her eyes meet the lens and she gives a soft smile. At about 00:04 she raises her right hand to the front of her chest and makes a small finger heart with her thumb and index finger, holding it below her chin so it never covers her face. She keeps smiling at the lens until the end.
```
