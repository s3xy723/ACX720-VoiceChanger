# -*- coding: utf-8 -*-
"""age_model.py —— 声音年龄的映射模型：任意源年龄 → 任意目标年龄。

为什么单独一个模块
------------------
"变声到某个年龄"不是一个参数，而是**四个独立参数**同时变：

    r     基频比      F0  → 声带长度/张力
    beta  共振峰比    声道长度 → 腔体大小
    breath 气声量     声门闭合不全 → 老年/幼年的"漏气感"
    jitter/shimmer    神经肌肉控制 → 抖动
    tilt  高频倾斜     组织松弛 → 高频衰减

把换算写在 Python 里，而不是写进 Verilog，理由有三条：
  1. 曲线上表都是浮点插值，硬件里做不了；
  2. 用户要"任意年龄"，参数表必须能随时改、能跑测试；
  3. FPGA 只需要 4 个定点数，接口稳定，RTL 不用跟着曲线一起改。

★ 差分原则（这条决定了"同年龄→同年龄"能不能做到无失真）
  所有量与"源年龄"作差，只有在**目标比源更老/更小**时才有量：
      r    = F0(目标) / F0(源)          —— 天然是比值，恒等时为 1
      beta = L(源) / L(目标)             —— 恒等时为 1
      breath = max(0, b(目标) - b(源))
  所以 a0 == a1 时全部归零，输出应当与输入**逐位相同**。这不是巧合，
  是设计出来的：没有"绝对年龄"，只有"相对变化"。
"""
import math

# ---------------------------------------------------------------- 曲线

# 会话基频（Hz，持续元音/朗读）。数据来自多项研究的合成，见
# docs/声音年龄_规律.md。按**对数**插值（音高是等比量）。
_F0_KNOTS = [
    # 年龄, 男,   女
    (5,   300, 300),   # 青春期前男女无差别
    (8,   285, 288),
    (10,  278, 282),
    (12,  250, 262),   # 性别差异从 12 岁开始
    (13,  200, 252),
    (14,  160, 240),   # 变声最剧烈
    (15,  140, 232),
    (16,  128, 226),
    (18,  122, 220),
    (25,  118, 216),
    (35,  114, 212),
    (45,  112, 210),   # 男性 F0 在 40~49 到最低点
    (55,  116, 205),
    (65,  124, 199),   # 之后男性回升、女性继续下降
    (75,  134, 193),
    (85,  146, 186),
]

# 声道长度（cm）。成年男 16.9 / 女 14.1（Fitch & Giedd 1999）；
# 儿童按声带/声道同比生长；老年基本不变，只留 1% 的缓慢下降。
_L_KNOTS = [
    (5,    9.5,  9.5),
    (8,   10.8, 10.8),
    (10,  11.8, 11.8),
    (12,  12.8, 12.8),
    (14,  15.0, 13.2),
    (16,  16.6, 13.9),
    (18,  16.9, 14.1),
    (30,  17.0, 14.2),
    (45,  17.0, 14.2),
    (60,  17.1, 14.3),
    (85,  17.2, 14.4),
]

AGE_MIN, AGE_MAX = 5, 85

# 变调器的安全区间：r = 1 + dphase/64，dphase ∈ [-30, 60]
# （见 rtl/voice_changer_top.v 的注释：r < 0.53 时双抽头相位不相干，
#   实测 r=0.375 有 3.8% 的频率误差 ≈ 65 音分）
R_MIN, R_MAX = 0.55, 1.90


def _clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _interp(knots, age, sex):
    """在 knots 上按 log 域插值。sex: "m" / "f"。"""
    age = _clip(float(age), AGE_MIN, AGE_MAX)
    col = 1 if sex == "m" else 2
    xs = [k[0] for k in knots]
    if age <= xs[0]:
        return float(knots[0][col])
    if age >= xs[-1]:
        return float(knots[-1][col])
    for i in range(len(knots) - 1):
        a0, a1 = xs[i], xs[i + 1]
        if a0 <= age <= a1:
            y0, y1 = math.log(knots[i][col]), math.log(knots[i + 1][col])
            t = (age - a0) / float(a1 - a0)
            return math.exp(y0 + (y1 - y0) * t)
    return float(knots[-1][col])


def f0_of(age, sex="m"):
    """该年龄的会话基频（Hz）。"""
    return _interp(_F0_KNOTS, age, sex)


def vtl_of(age, sex="m"):
    """该年龄的声道长度（cm）。"""
    return _interp(_L_KNOTS, age, sex)


# ---------------------------------------------------------------- 音质曲线

def _breath_base(age, sex):
    """绝对气声量 0..1（不是差分）。幼儿控制差、老年声门闭合不全。"""
    child = _clip((15.0 - age) / 9.0, 0.0, 1.0) * 0.45
    old = _clip((age - 52.0) / 33.0, 0.0, 1.0) * 0.85
    return _clip(max(child, old), 0.0, 1.0)


def _unstable(age, sex):
    """绝对不稳定度 0..1（jitter/shimmer 的驱动量）。"""
    child = _clip((14.0 - age) / 8.0, 0.0, 1.0) * 0.85
    old = _clip((age - 58.0) / 27.0, 0.0, 1.0)
    return _clip(max(child, old), 0.0, 1.0)


def _tilt_db(age, sex):
    """高频倾斜（3.5 kHz 以上的一阶搁架，dB）。

    孩子：腔体小、声带薄 → 高频更亮（正）。老年：组织松弛、黏膜弹性下降
    → 高频更暗（负）。
    """
    return 3.0 * _clip((16.0 - age) / 11.0, 0.0, 1.0) \
        - 7.0 * _clip((age - 55.0) / 30.0, 0.0, 1.0)


# ---------------------------------------------------------------- 主入口

def params(age_from, age_to, sex="m", strength=1.0):
    """源年龄 → 目标年龄，返回给硬件的四个参数（都已做差分与限幅）。

    sex       : "m" 男 / "f" 女。跨青春期时它决定目标音高走哪条曲线。
    strength  : 0..1 总强度（界面上的"幅度"滑条），用来做 A/B 试听。

    返回 dict：
        r        基频比（1.0 = 不变）
        beta     共振峰比（1.0 = 不变，>1 = 腔体变小/更童声）
        dphase   = round(64*(r-1))，直接喂 rtl/pitch_shift.v
        breath   0..1   气声注入量
        jitter   %      基频抖动
        shimmer  %      幅度抖动
        tilt_db  dB     高频倾斜（正=更亮）
        clipped  bool   r 是否被安全区间夹过
    """
    sex = "f" if str(sex).lower().startswith("f") else "m"
    s = _clip(float(strength), 0.0, 1.0)

    r_raw = f0_of(age_to, sex) / max(f0_of(age_from, sex), 1e-9)
    r = _clip(r_raw, R_MIN, R_MAX)
    clipped = abs(r - r_raw) > 1e-6

    beta = vtl_of(age_from, sex) / max(vtl_of(age_to, sex), 1e-9)

    breath = max(0.0, _breath_base(age_to, sex) - _breath_base(age_from, sex))
    jit = max(0.0, (_unstable(age_to, sex) - _unstable(age_from, sex))) * 1.6
    shim = max(0.0, (_unstable(age_to, sex) - _unstable(age_from, sex))) * 4.0
    tilt = _tilt_db(age_to, sex) - _tilt_db(age_from, sex)

    # strength 作用在"变化量"上：1.0 全量，0 则回到恒等
    r = 1.0 + (r - 1.0) * s
    beta = 1.0 + (beta - 1.0) * s
    breath *= s
    jit *= s
    shim *= s
    tilt *= s

    return {
        "r": r,
        "beta": beta,
        "dphase": int(round(64.0 * (r - 1.0))),
        "breath": breath,
        "jitter": 0.35 + jit,          # 基线 0.35%（健康成人）
        "shimmer": 2.0 + shim,
        "tilt_db": tilt,
        "clipped": clipped,
        "f0_from": f0_of(age_from, sex),
        "f0_to": f0_of(age_to, sex),
    }


# ---------------------------------------------------------------- 定点打包
#
# FPGA 侧只认这几个整数，格式定死，RTL 与上位机都必须按这张表来。
#   字段          位宽  格式          范围
#   beta16        16    Q1.14         0.5 .. 2.0  -> 8192..32768
#   dphase8        8    signed        -64..+63   （=64*(r-1)，实际限 -30..60）
#   breath8        8    Q0.8          0 .. 255
#   jitter8        8    Q0.8 %        0 .. 255   （单位 0.01%，1% -> 100）
#   shimmer8       8    Q0.8 %        0 .. 255
#   tilt8          8    signed dB*8   -128..127  （-16..+15.9 dB）

def pack(p):
    """把 params() 的结果打成 6 个字节，对应 FPGA 的配置寄存器。"""
    return {
        "beta16": int(_clip(round(p["beta"] * 16384.0), 8192, 32768)),
        "dphase8": int(_clip(int(p["dphase"]), -128, 127)),
        "breath8": int(_clip(round(p["breath"] * 255.0), 0, 255)),
        "jitter8": int(_clip(round((p["jitter"] - 0.35) * 100.0), 0, 255)),
        "shimmer8": int(_clip(round((p["shimmer"] - 2.0) * 100.0), 0, 255)),
        "tilt8": int(_clip(round(p["tilt_db"] * 8.0), -128, 127)),
    }


# ---------------------------------------------------------------- 自检

def _selftest():
    ok = 0
    fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [PASS] %s %s" % (name, detail))
        else:
            fail += 1
            print("  [FAIL] %s %s" % (name, detail))

    print("age_model 自检")

    # 1) 恒等：源=目标，必须逐项归零（这是"任意年龄"的前提）
    for a in (5, 12, 20, 30, 45, 60, 75, 85):
        for sx in ("m", "f"):
            p = params(a, a, sx)
            chk("恒等 %d/%s" % (a, sx),
                abs(p["r"] - 1) < 1e-9 and abs(p["beta"] - 1) < 1e-9
                and p["breath"] == 0.0 and p["dphase"] == 0
                and abs(p["tilt_db"]) < 1e-9,
                "r=%.6f beta=%.6f dphase=%d" % (p["r"], p["beta"], p["dphase"]))

    # 2) 方向：变老 → 男声基频升、女声基频降（研究结论反直觉但明确）
    pm = params(30, 75, "m")
    pf = params(30, 75, "f")
    chk("男声变老基频升高", pm["r"] > 1.0, "r=%.3f (+%.1f 半音)"
        % (pm["r"], 12 * math.log(pm["r"], 2)))
    chk("女声变老基频降低", pf["r"] < 1.0, "r=%.3f (%.1f 半音)"
        % (pf["r"], 12 * math.log(pf["r"], 2)))

    # 3) 变童声 → 基频升高 + 腔体变小，两个方向必须一致
    pc = params(30, 8, "m")
    chk("成人→儿童 基频升高且共振峰升高",
        pc["r"] > 1.2 and pc["beta"] > 1.2,
        "r=%.2f beta=%.2f (%.0f→%.0f Hz)"
        % (pc["r"], pc["beta"], pc["f0_from"], pc["f0_to"]))

    # 4) 气声/抖动只在变老时增加，反向不减（没法给声音"去气声"）
    chk("变老气声增加", pf["breath"] > 0.1, "breath=%.2f" % pf["breath"])
    chk("变年轻气声归零", params(75, 30, "f")["breath"] == 0.0)

    # 5) 限幅：儿童→成年男 需要 r≈0.4，必须被夹到安全区并标记
    pe = params(6, 40, "m")
    chk("极端跨龄被限幅并标记", pe["clipped"] and 0.54 < pe["r"] < 0.56,
        "r=%.3f dphase=%d" % (pe["r"], pe["dphase"]))

    # 6) strength=0 必须回到恒等
    p0 = params(30, 80, "f", strength=0.0)
    chk("strength=0 恒等", abs(p0["r"] - 1) < 1e-9 and p0["breath"] == 0.0)

    # 7) 定点打包往返：beta 误差 < 0.1%，dphase 与 r 一致
    p = params(35, 70, "m")
    pk = pack(p)
    chk("beta 定点误差 < 0.1%",
        abs(pk["beta16"] / 16384.0 - p["beta"]) / p["beta"] < 1e-3,
        "beta=%.4f -> %d" % (p["beta"], pk["beta16"]))
    chk("dphase 与 r 自洽",
        abs((1 + pk["dphase8"] / 64.0) - p["r"]) < 0.01,
        "dphase=%d -> r=%.3f (目标 %.3f)"
        % (pk["dphase8"], 1 + pk["dphase8"] / 64.0, p["r"]))

    print("\n  PASS = %d  FAIL = %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
