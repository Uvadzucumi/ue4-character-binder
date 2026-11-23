# UE4 Character Binder

**[English](#english)** | **[Русский](#русский)**

---

<a name="english"></a>
## English

A Blender add-on (4.2 LTS and newer, tested up to 5.2.4) that binds a custom character mesh to the **standard UE4 mannequin skeleton** without retargeting and without modifying the skeleton's rest pose.

**Typical use case:** You have a custom character model (for example, a hunched goblin in an A-pose) with no rig, and your UE4 project already has the standard mannequin skeleton and an animation library. This add-on produces an FBX that imports into UE4 with **Use Existing Skeleton** — letting your character immediately play all existing mannequin animations.

* **License:** GPL-2.0
* **Single file:** `ue4_character_binder.py`

---

### How It Works (Temporary-Pose Binding)

**The classic problem:** Blender's automatic weights (Bone Heat) are calculated based on the current positions of the bones. The mannequin skeleton stands in its standard pose (T/A-pose), while your character has its own unique pose and proportions (hunched, wider stance, different limb lengths). Binding directly to an unposed skeleton produces poor weights that bleed onto wrong bones and deform joints badly.

The add-on solves this in 5 steps:

1. **Markers:** Creates a set of empty markers (`M_pelvis`, `M_hand_l`, etc.). You place them onto the joints of **your character**.
2. **Temporary Posing:** Temporarily poses the skeleton to match the markers — every joint snaps directly onto its marker, while bones rotate and stretch uniformly (in Pose Mode only; rest geometry is untouched).
3. **Auto-Weights in Bent Pose:** Computes automatic weights while the bones align with your character's joints, allowing Bone Heat to distribute weights accurately.
4. **Pose Reset:** Resets the skeleton back to the standard UE4 rest pose. Your mesh retains its authored shape (the goblin stays hunched) with accurate skin weights already baked in.
5. **FBX Export:** The exported FBX contains the standard mannequin rest pose, allowing UE4 to accept the mesh out-of-the-box onto your existing skeleton.

**Key guarantees:**
* Rest geometry of the skeleton bones is **never modified** — only the pose layer changes temporarily and is reset after binding.
* To move every joint freely, bone chains are temporarily disconnected from parent tails (`use_connect=False`) and welded back upon reset, bind, or export.
* Bone stretching is strictly **uniform** across all axes, preventing hierarchy shear.

---

### Installation

1. Save `ue4_character_binder.py` anywhere on your disk.
2. In Blender: `Edit > Preferences > Add-ons` (or `Get Extensions` dropdown arrow in 4.2+) → **Install from Disk...** → select the file and enable **UE4 Character Binder**.
3. Open the 3D Viewport, press `N`, and select the **UE4 Binder** tab.

*When updating:* Remove the old version from the Add-ons list, install the new file, and restart Blender (or reload the `.blend` file) to clear old registrations.

---

### Scene Preparation

1. Import the UE4 mannequin (`File > Import > FBX`) exported from UE4 with its mesh (`SK_Mannequin`). Both the armature and skeletal mesh will appear in the scene.
2. **Delete the mannequin mesh, keep the Armature** — this is your target skeleton.
3. Import your custom character (OBJ/FBX). It must have no armature and be in an A-pose or its natural rest pose. Roughly scale it to match the skeleton (or use step `1b` to match height automatically).
4. Do not edit mannequin bones in Edit Mode — the rest geometry must remain canonical.

*Bone naming check:* The skeleton must be a standard UE4 mannequin (`pelvis`, `spine_01..03`, `neck_01`, `head`, `clavicle_l/r`, `upperarm_l/r`, `lowerarm_l/r`, `hand_l/r`, `thigh_l/r`, `calf_l/r`, `foot_l/r`, `ball_l/r`, etc.). Step 1 verifies this automatically.

---

### Add-on Panel Overview

#### Targets
| Field | Description |
|---|---|
| **UE4 Armature** | The mannequin armature (target skeleton). |
| **Character Mesh** | Your custom character mesh. |

#### Options
| Option | Default | Description |
|---|---|---|
| **Pose Thumbs With Markers** | `OFF` | Creates 6 extra markers for thumb chains and poses them. When `OFF`, thumbs simply follow the posed hand. |
| **Merge Fingers Into Hands** | `ON` | Transfers `index/middle/ring/pinky` weights into `hand_l`/`hand_r` and deletes finger vertex groups after binding (useful if your character has mitten hands or no separate finger geometry). |
| **Reset Pose After Bind** | `ON` | Immediately resets the skeleton to the standard rest pose after binding. |

---

### Pipeline Steps

**Recommended order:** `1 → 2 → (place markers) → 3 → 4 → 5a → 5b → 7` (`5`, `5c`, `5d` are alternative/helper steps).

#### 1. Check Setup
Validates that the armature is a UE4 mannequin (checks all required bones), checks mesh state (existing parent or vertex groups), detects unapplied transforms (rotation/scale are auto-applied during bind), and compares skeleton vs. mesh height. Prints a detailed report in a popup and the System Console.

#### 1b. Fit Mesh Height to Skeleton (optional)
Scales the mesh bounding box height to match the skeleton height. A rough starting point before placing markers.

#### 2. Create Joint Markers
Creates the `UE4_JointMarkers` collection with 22 markers (`M_pelvis`, `M_spine_01`, `M_head`, `M_hand_l`, `M_thigh_l`, etc.), plus 6 thumb markers if **Pose Thumbs** is enabled.

**Next action:** **Move each marker onto the corresponding joint of your character.**
* Place markers inside/on the mesh at the actual joint pivot — depth matters, so check from front, side, and top views.
* Tip: Use `Shift+S` (Snap) or Blender's Snapping tool (`Volume` / `Face`) while moving markers.
* Re-running step 2 recreates the markers at their default skeleton positions.
* Duplicate markers (`M_hand_l.001`) are handled automatically (the one moved farther from the skeleton joint takes priority).

#### 3. Mirror Markers (L -> R)
Copies left-side marker positions to the right side, mirroring across the **skeleton's own symmetry plane** (computed from symmetric bone pairs, so it works regardless of FBX import orientation). Missing right-side markers are recreated automatically. Center markers are untouched.

#### 4. Pose Skeleton to Markers
Temporarily poses the skeleton so every chain joint snaps **exactly** onto its marker (translates head, rotates bone, and stretches uniformly to the next marker). Terminal bones (`head`, `hand_l/r`, `ball_l/r`) preserve the mannequin's rest angle while snapping their heads to their markers.

**Diagnostics (System Console):**
* Lists any unmatched or missing markers.
* **Floating marker check:** Flags any marker farther than ~12% of skeleton height from the mesh surface.
* Reports post-pose joint-to-marker deviations (should be `~0`).

#### 5. Bind (Auto Weights) — Standard Binding
One-click binding: parents the mesh and runs Bone Heat across the entire mesh in the bent pose, optionally merges fingers, normalizes/limits weights to 4 influences per vertex, and resets the pose.
* Automatically applies mesh rotation/scale.
* **Automatically excludes IK bones (`ik_*`)** and deletes any stale IK vertex groups.
* Best for simple single-layer characters. For multi-layered outfits or to prevent weight bleed between limbs, use `5a + 5b`.

#### 5a. Auto Split Parts (Nearest Bone)
Automatically generates weight masks (`vb:arms`, `vb:legs`, `vb:torso`, `vb:head`, `vb:hands`, `vb:feet`) by assigning each vertex to the closest body part in rest pose. You can refine these vertex groups manually in Edit Mode (`Assign` / `Remove`) before running `5b`.

#### 5b. Bind Masked Weights — Part-by-Part Binding (Recommended)
Runs isolated Bone Heat passes for each `vb:` mask, restricting each pass strictly to that body part's bones and vertices.

**Benefits:**
* **Zero cross-limb weight bleed:** Arm bones physically cannot grab leg vertices and vice versa.
* **Multi-layered geometry support:** Straps, bandages, vests, and underlying body layers bind cleanly without leaving inner layers unweighted.
* **Automatic fallback:** If Bone Heat fails on a complex/enclosed sub-part, that pass automatically falls back to inverse-square distance weights so the bind completes cleanly.

**Supported masks:**
* `vb:<part>` — Standard parts: `arms`, `legs`, `torso`, `head`, `hands`, `feet`.
* `vb:<bone_name>` — Exclusive single-bone override (e.g., `vb:thigh_l`). Locked exclusively to that bone — ideal for pouches, holsters, belts, or rigid armor pieces.

#### 5c. Repair Zero Weights
Finds any remaining unweighted vertices and copies weights from the nearest weighted vertex via KD-tree. Vertices inside `vb:` masks only sample from the same mask, preventing cross-part leaks.

#### 5d. Clean IK Bone Weights
Removes all `ik_*` vertex groups from the mesh. Steps `5` and `5b` do this automatically; use `5d` if you bound the mesh manually or via third-party tools.

#### 6. Reset Skeleton to Standard Pose
Zeros out all pose transforms and restores welded bone connections (`use_connect`). Usually triggered automatically after binding.

#### 7. Export FBX for UE4
Forces a pose reset and performs a final sweep to strip any stray IK weights before exporting the armature and mesh to FBX (`//character_ue4.fbx` by default) with `add_leaf_bones=False` and `bake_anim=False`.

---

### Why IK Bones Must Have Zero Weights

Bones like `ik_foot_root`, `ik_foot_l`, `ik_foot_r`, `ik_hand_root`, `ik_hand_gun`, `ik_hand_l`, and `ik_hand_r` are **animation-driven** bones in UE4. The engine moves them dynamically every frame for foot placement and weapon constraints. Any skin weight on these bones will pull and distort your mesh during playback.

The add-on enforces clean IK bones at every stage (`5`, `5b`, `5d`, and `7`), matching any bone containing the `ik` token (`ik_foot_l`, `foot_ik_l`, etc.).

---

### Importing into UE4

1. Export the FBX using **Step 7**.
2. In UE4: `Content Browser > Import` and select your `.fbx` file.
3. In the FBX Import Options dialog:
   * **Skeleton:** Select your existing UE4 Mannequin skeleton (*Use Existing Skeleton*).
   * **Import Mesh:** Enabled; **Import Animations:** Disabled.
   * Keep **Import Uniform Scale** at `1.0`.
4. Open the newly imported Skeletal Mesh, assign the mannequin's Physics Asset if needed, and test with your existing Animation Blueprints or sequences.

---

### Troubleshooting

| Symptom | Cause & Solution |
|---|---|
| `Automatic weights failed (Bone Heat solver)` on Step 5 | Non-manifold geometry, holes, or overlapping parts. Use `5a + 5b`, which isolates parts and falls back to distance weights on tricky subsets. |
| Arm weights bleeding onto legs (or vice versa) | Classic Bone Heat spillover. Run `5a + 5b` to isolate body parts. |
| Outer clothing/straps steal weights, leaving body underneath unweighted | Multi-layer geometry. Separate inner and outer layers into different `vb:` masks, run `5b`, then run `5c`. |
| `Mesh is already parented to this armature` | Unbind first: `Alt+P > Clear Parent and Keep Transform`, remove the Armature modifier, and re-bind. |
| `Markers are still at the skeleton joints - nothing to pose` | Markers were not moved after Step 2. Position them on your character's joints first. |
| Twisted or broken pose after Step 4 | Check the System Console for floating markers far from the mesh surface and adjust their 3D depth. |
| IK bones have weights after manual skinning | Click `5d. Clean IK Weights` to remove all `ik_*` vertex groups. |

---
---

<a name="русский"></a>
## Русский

Плагин для Blender (4.2 LTS и новее, проверен вплоть до 5.2.4), который привязывает кастомный персонаж к **стандартному скелету UE4-манекена** без ретаргетинга и без правки rest-позы скелета.

**Типичный сценарий:** у вас есть модель персонажа (например, горбатый гоблин в A-позе) без скелета, а в проекте UE4 уже есть скелет манекена и библиотека анимаций. Плагин позволяет получить FBX, который импортируется в UE4 с опцией **Use Existing Skeleton** — и персонаж сразу играет все готовые анимации манекена.

* **Лицензия:** GPL-2.0
* **Один файл:** `ue4_character_binder.py`

---

### Как это работает (принцип temporary-pose binding)

**Классическая проблема:** автовеса Blender (Bone Heat) вычисляются по текущему положению костей. Скелет манекена стоит в стандартной позе (T/A-pose), а ваш персонаж — в своей собственной (горбатый, A-поза, другие пропорции). Если просто привязать меш к прямому скелету, веса получаются плохими: вес попадает не на те кости, суставы деформируются неправильно.

Плагин решает это за 5 шагов:

1. **Маркеры.** На сцену создаётся набор пустышек-маркеров (`M_pelvis`, `M_hand_l`, ...). Вы расставляете их на суставы **вашего персонажа**.
2. **Временная позировка.** Скелет временно позируется под маркеры: каждый сустав точно встаёт на свой маркер, кости поворачиваются и равномерно растягиваются (только в pose-режиме — rest-геометрия не меняется).
3. **Автовеса в согнутой позе.** Привязка меша выполняется, когда кости совпадают с суставами персонажа — Bone Heat видит «правильное» положение костей и распределяет веса корректно.
4. **Сброс позы.** Скелет возвращается в стандартную rest-позу UE4. Меш при этом сохраняет свою форму (гоблин остаётся горбатым), а веса уже посчитаны.
5. **Экспорт FBX.** Rest-поза в файле — стандартный скелет манекена, поэтому UE4 принимает меш «как есть» на существующий скелет.

**Ключевые гарантии:**
* Rest-геометрия костей скелета **никогда не изменяется** — правится только поза, и она сбрасывается после привязки.
* Для свободного перемещения суставов цепочки костей временно отсоединяются от родительских хвостов (`use_connect=False`) и свариваются обратно при сбросе/привязке/экспорте.
* Растяжение костей — только **равномерное** по всем осям, поэтому иерархия не перекашивается (shear исключён).

---

### Установка

1. Сохраните файл `ue4_character_binder.py` в любое место.
2. В Blender: `Edit > Preferences > Add-ons` (в Blender 4.2+ через меню в правом верхнем углу `Install from Disk...`) — выберите файл и включите аддон **UE4 Character Binder**.
3. Откройте 3D Viewport, нажмите `N` — появится вкладка **UE4 Binder**.

*При обновлении аддона:* удалите старую версию в списке Add-ons, установите новый файл и перезапустите Blender (или переоткройте `.blend`), чтобы сбросились прежние регистрации.

---

### Подготовка сцены

1. Импортируйте манекен UE4 (`File > Import > FBX`) — экспорт скелета из UE4 вместе с мешем (`SK_Mannequin`). В сцене появятся armature и skeletal mesh.
2. **Удалите меш манекена. Armature оставьте** — это целевой скелет.
3. Импортируйте своего персонажа (OBJ/FBX). Он должен быть без скелета, в A-позе или своей естественной позе. Примерно отмасштабируйте его под скелет (шаг `1b` сделает это автоматически по высоте).
4. Не редактируйте кости скелета манекена в Edit Mode — rest-геометрия должна остаться эталонной.

*Проверка имен костей:* скелет должен быть стандартным UE4-манекеном (`pelvis`, `spine_01..03`, `neck_01`, `head`, `clavicle_l/r`, `upperarm_l/r`, `lowerarm_l/r`, `hand_l/r`, `thigh_l/r`, `calf_l/r`, `foot_l/r`, `ball_l/r` и т.д.). Шаг 1 это проверит.

---

### Панель аддона

#### Targets
| Поле | Назначение |
|---|---|
| **UE4 Armature** | Armature манекена (целевой скелет). |
| **Character Mesh** | Меш вашего персонажа. |

#### Options
| Опция | По умолчанию | Что делает |
|---|---|---|
| **Pose Thumbs With Markers** | `off` | Создавать маркеры для больших пальцев и позировать их цепочки (плюс 6 маркеров). Выкл. — большие пальцы просто следуют за рукой. |
| **Merge Fingers Into Hands** | `on` | После привязки перенести веса `index/middle/ring/pinky` в `hand_l/hand_r` и удалить группы пальцев (полезно, если на персонаже нет отдельной геометрии пальцев). |
| **Reset Pose After Bind** | `on` | Сразу после привязки сбросить скелет в стандартную позу. |

---

### Шаги пайплайна

**Порядок:** `1 → 2 → (расставить маркеры) → 3 → 4 → 5a → 5b → 7` (`5`, `5c`, `5d` — вспомогательные).

#### 1. Check Setup
Проверяет: является ли скелет UE4-манекеном (все обязательные кости), состояние меша (не привязан ли уже, есть ли вертекс-группы), неприменённые трансформации (rotation/scale будут автоматически применены на шаге `5`/`5b`), соотношение высот скелета и меша. Отчёт выводится во всплывающем сообщении и в System Console.

#### 1b. Fit Mesh Height to Skeleton (optional)
Масштабирует меш по высоте bounding box под высоту скелета. Грубая оценка «на глазок»: детали всё равно исправляются позированием, но порядок величин лучше выдержать.

#### 2. Create Joint Markers
Создаёт коллекцию `UE4_JointMarkers` и 22 маркера (`M_pelvis`, `M_spine_01`, `M_head`, `M_hand_l`, `M_thigh_l` ...). С опцией **Pose Thumbs** — ещё 6 маркеров больших пальцев.

**Что делать дальше:** **перетащите каждый маркер на соответствующий сустав вашего персонажа.**
* Маркер должен лежать **на поверхности/внутри меша** в месте сустава — глубина важна. Проверяйте со всех сторон (вид сбоку, сверху).
* Быстрый способ: `Shift+S` (Snap → Cursor to Selected / Selected to Cursor), режим Edit Mode у меша + снап, или магнит-снап при перемещении.
* Шаг 2 можно запускать повторно: старые маркеры пересоздаются в стандартных местах.
* Дубликаты маркеров (`M_hand_l.001`) обрабатываются корректно: если таких два, берётся тот, что дальше сдвинут от сустава скелета.

#### 3. Mirror Markers (L -> R)
Копирует позиции левых маркеров на правые, зеркалируя их относительно плоскости симметрии **самого скелета** (плоскость вычисляется по симметричным парам костей, поэтому работает при любой ориентации FBX-импорта). Если правого маркера нет — он создаётся автоматически. Центральные маркеры не трогаются.

#### 4. Pose Skeleton to Markers
Временно позирует скелет: каждый сустав цепей **точно** встаёт на свой маркер (сустав сдвигается, кость вращается вокруг сустава и равномерно растягивается до следующего маркера). Последняя кость каждой цепи (`head`, `hand_l/r`, `ball_l/r`) сохраняет угол манекена — только её голова встаёт на маркер.

**Диагностика (System Console):**
* Какие маркеры не нашлись / не совпали с костями.
* **Проверка «висящих» маркеров:** каждый маркер, расположенный дальше ~12% высоты скелета от поверхности меша, попадёт в список — сдвиньте его на меш.
* После позировки — отклонения суставов от маркеров (в норме `~0`).

#### 5. Bind (Auto Weights) — классическая привязка
Быстрый путь одним нажатием: parent + Bone Heat по всему мешу в текущей (согнутой) позе, опционально merge пальцев, нормализация/ограничение до 4 весов на вершину, затем сброс позы.
* Трансформации меша (rotation/scale) автоматически применяются.
* **IK-кости (`ik_*`) автоматически исключаются** из раздачи весов и их старые вертекс-группы удаляются.
* Подходит для простых однослойных персонажей. Для многослойной геометрии и защиты от «переползания» весов используйте `5a + 5b`.

#### 5a. Auto Split Parts (Nearest Bone)
Автоматически создаёт весовые маски `vb:arms`, `vb:legs`, `vb:torso`, `vb:head`, `vb:hands`, `vb:feet`: каждая вершина меша попадает в маску той части тела, к чьей кости она ближе всего (rest-поза). Дальше группы можно править вручную в Edit Mode (выделить вершины → `Assign`). После этого запускайте `5b`.

#### 5b. Bind Masked Weights — привязка по маскам (рекомендуется)
Главный инструмент качества. Привязка выполняется **по частям тела**: для каждой `vb:`-маски запускается свой Bone Heat проход, в котором участвуют только кости этой части и только вершины этой маски.

**Что это даёт:**
* **Веса не переползают между частями:** кости рук физически не могут получить веса на вершинах ног и наоборот.
* **Многослойная геометрия:** повязка и нога под ней, жилет и туловище и т.п. — если слои в разных масках, каждый слой считается отдельно, и слой под повязкой больше не остаётся без весов.
* Если Bone Heat падает на сложной подгруппе (замкнутые слои, самопересечения), проход автоматически заменяется дистанционными весами — привязка не отменяется.

**Маски:**
* `vb:<часть>` — стандартные части: `arms`, `legs`, `torso`, `head`, `hands`, `feet`.
* `vb:<имя_кости>` — жёсткий override на одну кость, например `vb:thigh_l`: отмеченные вершины будут привязаны только к этой кости (и исключаются из всех остальных проходов). Полезно для ремней, сумок, элементов брони.

#### 5c. Repair Zero Weights
Ищет вершины без весов и копирует им веса ближайшей взвешенной вершины (KD-tree). Вершины внутри `vb:`-масок получают веса только от вершин той же маски — утечки между частями нет. Полезно после ручных правок или для добивки слоя «под повязкой».

#### 5d. Clean IK Bone Weights
Удаляет все вертекс-группы `ik_*` с меша. Шаги `5` и `5b` делают это автоматически; `5d` нужен, если вы привязывали меш сторонним способом и хотите вычистить веса IK вручную.

#### 6. Reset Skeleton to Standard Pose
Сброс позы скелета в стандартную UE4 rest-позу (все pose-трансформы в ноль) + восстановление сваренных соединений костей. Обычно вызывается автоматически после привязки.

#### 7. Export FBX for UE4
Перед экспортом принудительно сбрасывает позу и **снимает остатки IK-весов** (с предупреждением), затем экспортирует armature + mesh в FBX (`//character_ue4.fbx` по умолчанию) с `add_leaf_bones=False` и `bake_anim=False`.

---

### IK-кости: почему без весов

Кости `ik_foot_root`, `ik_foot_l`, `ik_foot_r`, `ik_hand_root`, `ik_hand_gun`, `ik_hand_l`, `ik_hand_r` — это **анимационные** кости UE4: система анимации двигает их каждый кадр. Если на них есть skin weights, меш начнёт «дёргаться» за ними и ломать анимации. Плагин автоматически исключает и вычищает любые кости с токеном `ik` на шагах `5`, `5b`, `5d` и `7`.

---

### Импорт в UE4

1. Экспортируйте FBX шагом **7**.
2. В UE4: `Content Browser > Import`, выберите файл.
3. В диалоге импорта:
   * **Skeleton** — выберите существующий скелет манекена (*Use Existing Skeleton*).
   * **Import Mesh** — включён; **Import Animations** — выключен.
   * **Import Uniform Scale** — `1.0`.
4. Откройте новый Skeletal Mesh, назначьте Physics Asset манекена при необходимости и запускайте любые анимации манекена через Anim Blueprint / Slot.

---

### Частые проблемы и решения

| Симптом | Причина / решение |
|---|---|
| «Automatic weights failed (Bone Heat solver)» на шаге 5 | Дырки, несвязные части или пересечения геометрии. Используйте `5a + 5b` — там неудача heat на подгруппе заменяется дистанционными весами. |
| Веса рук переползают на ноги (и наоборот) | Классический spillover Bone Heat. Запустите `5a + 5b`: каждая часть считается в своём проходе. |
| Повязка/жилет «перехватили» веса, а слой под ними пуст | Многослойная геометрия. Разнесите слои по разным `vb:` маскам, запустите `5b`, затем `5c`. |
| «Mesh is already parented to this armature» | Меш уже привязан: `Alt+P > Clear Parent and Keep Transform` и удалите Armature-модификатор, затем повторите привязку. |
| «Markers are still at the skeleton joints - nothing to pose» | Маркеры не сдвинуты с суставов скелета. Расставьте их на персонаже после шага 2. |
| Поза после шага 4 кривая | Смотрите System Console: список «маркеров далеко от поверхности меша». Проверьте глубину маркеров сбоку/сверху. |
| Веса на IK-костях после ручного бинда | Нажмите `5d` (Clean IK Weights) — группы `ik_*` удалятся. |
