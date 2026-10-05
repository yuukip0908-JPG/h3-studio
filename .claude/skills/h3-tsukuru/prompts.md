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


## D. 髪をかき上げる（YUKI NAK @HashedAIvideo 2026-09-21 の型。2026-10-05 実測 282秒）

結果: 両手で髪をかき上げ、最後に微笑む。顔はずっと sakura のまま。一瞬だけ手が額にかかる。
頭の文の最後は「her hair only moves with her head and hands」にする。

```
She looks at the lens with a calm expression. At about 00:00.8 she raises both hands to her temples and slowly sweeps her hair back over the top of her head, her fingers sliding through the long black hair, taking about 2 seconds. Her chin lifts slightly and her eyes half close. Her hands stay at the sides of her head and never cover her face. She holds her hands in her hair for a moment, then opens her eyes and looks straight into the lens. At about 00:04 she slowly lowers her hands to her sides, her hair falling naturally over her shoulders, and gives a soft closed-lip smile that stays until the end. Every movement is slow and unhurried. Her clothing never changes. No walking, no cuts, no zoom, no camera movement, no spinning, no props, no extra fingers, no plastic skin.

overall_soundscape: quiet room tone, faint street noise outside the window, the soft sound of fingers moving through hair.

non_diegetic_music: N/A
```

## E. K-POP の腰の8の字とウェーブ（Zeto @ZetoGroovin 2026-09-10 の Girl Crush テンプレ 3.75〜7.5秒の区間から。2026-10-05 実測 274秒）

結果: 腰が左右に流れ、最後に微笑む。ウェーブは弱め。腕を外へ大きく開きすぎ、終盤は手が画面の外へ出る。
次は「arms open only to shoulder width」と書く。

```
Girl crush K-pop mood. Her feet are shoulder-width apart and her knees are soft. At about 00:00.5 she draws one slow horizontal figure-eight with her hips, her weight shifting from her right leg to her left and back to center, her waist leading and her chest following slightly later, her left hand resting lightly on the side of her hip and her right hand drawing a small arc beside her body, taking about 2.5 seconds. Then, pushing through the floor, a slow body wave rises from her hips through her stomach and chest to her shoulders, taking about 1.5 seconds, her arms following the wave up to the front of her chest and opening outward. At about 00:04.5 she lifts her eyes to the lens and gives a brief, confident, slightly teasing smile, and keeps a gentle sway until the end. The camera stays fixed and never zooms in on her body. Her clothing never changes. No walking, no cuts, no zoom, no camera movement, no spinning, hands never cover the face, no props, no extra fingers, no plastic skin.

overall_soundscape: quiet room tone, soft fabric rustle.

non_diegetic_music: slow, sultry 95 BPM girl crush K-pop groove with a heavy kick, a warm bass and no vocals.
```

## ダンス・シリーズ（E が好評だった。2026-10-05 オーナー「ダンスいいね」）

共通の頭は D/E と同じ（「her hair only moves with her head and hands」）。共通の尻:

```
Every movement is smooth and flows into the next; nothing stops dead. Her arms open no wider than her shoulders and her hands always stay inside the frame. The camera stays fixed and never zooms in on her body. Her clothing never changes. No walking, no cuts, no zoom, no camera movement, no spinning, hands never cover the face, no props, no extra fingers, no plastic skin.

overall_soundscape: quiet room tone, soft fabric rustle.

non_diegetic_music: slow, sultry 95 BPM girl crush K-pop groove with a heavy kick, a warm bass and no vocals.
```

### E2. 腰の8の字とウェーブ（E の手直し）

```
Girl crush K-pop mood. Her feet are shoulder-width apart and her knees are soft. At about 00:00.5 she draws one slow horizontal figure-eight with her hips, her weight shifting from her right leg to her left and back to center, her waist leading and her chest following slightly later, her left hand resting on the side of her hip and her right hand drawing a small arc close to her body, taking about 2.5 seconds. Then, pushing through the floor, a clear body wave rolls upward from her hips through her stomach and chest and out through her shoulders and neck, taking about 1.5 seconds, her hands sliding up along the sides of her body to her waist. At about 00:04.5 she lifts her eyes to the lens and gives a brief, confident, slightly teasing smile, and keeps a gentle sway until the end.
```

### F. 胸の前で交差した腕をほどく（Zeto の振付モチーフ）

```
Girl crush K-pop mood. Her feet are shoulder-width apart and her knees are soft. She starts with her arms crossed in front of her chest. At about 00:00.5 her arms unfold in order, shoulders, then elbows, then wrists, opening diagonally downward to the sides of her hips, taking about 1.5 seconds, while her hips flow the opposite way so her whole body draws one long curve. Her weight moves from her left leg to her right. At about 00:02.5 her arms circle back to the front of her chest and she repeats the motif smaller, her hips flowing to the other side. At about 00:04.5 she looks straight into the lens with a calm, strong expression, then a small smile, and her shoulders keep a soft groove until the end.
```

### G. 左右のヒップアクセントと肩（Zeto の第4フレーズから）

```
Girl crush K-pop mood. Her feet are shoulder-width apart and her knees are soft, her hands resting lightly on the front of her thighs. On the beat, she makes two clear hip accents, left and then right, each about 0.5 seconds, her shoulders moving slightly the opposite way. She repeats the left and right accents once more, a little bigger. At about 00:03 her hands slide up to her waist and she lets her shoulders roll back once, slowly. At about 00:04.5 she tilts her head slightly, lifts her eyes to the lens and gives a confident, slightly teasing smile, bouncing softly in her knees to the groove until the end.
```
