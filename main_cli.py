# %%
import argparse
import json
import locale
import logging
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

import pulp as lp
from rich import box
from rich.console import CONSOLE_HTML_FORMAT, Console
from rich.logging import RichHandler
from rich.status import Status
from rich.table import Column, Table
from rich.terminal_theme import MONOKAI

from download_data import download_data
from gf_utils import GameData
from load_user_info import load_perfect_info, load_user_info
from prepare_choices import prepare_choices

logger = logging.getLogger()

console = Console(record=True)
logging.basicConfig(
    format="%(message)s",
    handlers=[RichHandler(console=console, show_time=False, show_path=False)],
)
os.chdir(Path(__file__).resolve().parent)
# %% argparse
parser = argparse.ArgumentParser()
parser.add_argument(
    "theater_id", default="848", type=int, help="关卡id,如736代表第7期高级区第6关"
)
parser.add_argument(
    "-d", "--delete_data",
    action="store_true",
    help="删除现有数据文件，强制重新下载"
)
parser.add_argument(
    "-e",
    "--encoding",
    type=str,
    nargs="*",
    default=["utf-8", "gbk"],
    help="用于读取user_info的编码，默认仅尝试utf-8和gbk",
)
parser.add_argument("-m", "--max_dolls", type=int, default=30, help="上场人数")
parser.add_argument(
    "-f", "--fairy_ratio", type=float, default=2, help="妖精加成,默认4个5星1+0.25*4=2倍"
)
parser.add_argument(
    "-u",
    "--upgrade_resource",
    type=int,
    default=0,
    help="可以用于强化的资源量（普通装备消耗1份，专属消耗3份）"
)
parser.add_argument(
    "-r", "--region", 
    type=str, 
    default="ch",
    choices=['ch', 'tw', 'kr', 'jp', 'us'],
    help="ch/tw/kr/jp/us"
)
parser.add_argument(
    "-p", "--perfect", action="store_true", help="使用完美仓库（满婚满级满技满装备）"
)
parser.add_argument("-t", "--type_sort", action="store_true", help="按枪种排序")
parser.add_argument(
    "-i", "--input",
    type=Path,
    default=Path("./info/user_info.json"),
    help="自定义用户数据路径"
)
args = parser.parse_known_args()[0]
# %% Start
with Status("Initializing", console=console, spinner="bouncingBar") as status:
    # %% 战区关卡参数
    theater_id = args.theater_id
    fairy_ratio = args.fairy_ratio  # 妖精加成：5星1.25
    max_dolls = args.max_dolls  # 上场人数
    region = args.region  # 服务器
    use_perfect = args.perfect  # 完美梯队
    upgrade_resource = (
        args.upgrade_resource if not use_perfect else 999
    )  # 可以用于强化的资源量（普通装备消耗1份，专属消耗3份）

    # %%
    status.update("Downloading data")
    if args.delete_data:
        shutil.rmtree("./data")
    download_data(dir="./data", region=region)
    game_data = GameData(f"data/{region}")
    gun_info, equip_info = game_data["gun"], game_data["equip"]

    status.update("Reading user info")
    if use_perfect:
        user_gun, user_equip = load_perfect_info(game_data)
    else:
        user_info_file = args.input
        encoding_options = {*args.encoding, locale.getpreferredencoding()}
        for encoding in encoding_options:
            try:
                data = user_info_file.read_text(encoding=encoding)
            except UnicodeDecodeError as e:
                logger.warning(e)
            else:
                user_info = json.loads(data)
                break
        else:
            raise RuntimeError(
                "Failed to open user_info.json with all encoding options"
            )
        user_gun, user_equip = load_user_info(user_info, game_data)
    status.update("Forming problem")
    choices = prepare_choices(
        user_gun, user_equip, theater_id, max_dolls, fairy_ratio, game_data
    )

    # %%
    status.update("Solving")
    resource = {}
    for id, _ in user_gun.items():
        resource[f"g_{id}"] = 1
    for eid, equip in user_equip.items():
        resource[f"e{eid}_10"] = equip["level_10"]
        resource[f"e{eid}_0"] = equip["level_00"]
    resource["count"] = max_dolls
    resource["score"] = 0
    resource["upgrade"] = upgrade_resource
    lp_vars = {}
    coeff_lp_var_dict = defaultdict(list)
    problem = lp.LpProblem("battlefield", lp.LpMaximize)
    for k, recipe in choices.items():
        lp_vars[k] = lp.LpVariable(k, cat=lp.LpInteger, lowBound=0)
        for r, c in recipe["content"].items():
            # build a dict with value as lists of (coefficient, LpVar) tuples before building LpAffineExpression in bulk
            # else doing += coefficient*LpVar would trigger significantly amount of costly LpAffineExpression.__init__ call
            coeff_lp_var_dict[r].append((lp_vars[k], c))
    for k, v in coeff_lp_var_dict.items():
        resource[k] += lp.LpAffineExpression(v)
    for k, v in resource.items():
        problem += v >= 0, k
    problem += resource["score"] + 0.001 * resource["upgrade"]

    lp_bin: Path = (
        Path(os.getcwd())
        / "solverdir"
        / "cbc"
        / lp.operating_system
        / lp.arch
        / lp.LpSolver_CMD.executableExtension("cbc")
    )
    problem.solve(lp.COIN_CMD(msg=0, path=str(lp_bin)))
    # %%
    status.update("Done")
    if console.width < 60:
        console.width = 1000
    box_per_row = min(5, (console.width - 10) // 25)

    u_info, g_info = [], []
    for k, v in lp_vars.items():
        if v.value() > 0:
            if k[0] == "u":
                u_info.append([choices[k]["info"], v])
            else:
                g_info.append([choices[k]["info"], v])
    u_info.sort(
        key=lambda x: 0.001 * v.value() - equip_info[x[0]["eid"]]["exclusive_rate"],
        reverse=True,
    )
    if not args.type_sort:
        g_info.sort(key=lambda x: x[0]["score"], reverse=True)
    else:
        g_info.sort(
            key=lambda x: (gun_info[int(x[0]["gid"])]["type"], int(x[0]["gid"]))
        )

    rank_color = {
        1: "magenta",
        2: "white",
        3: "cyan",
        4: "green",
        5: "yellow",
        6: "red",
        7: "magenta",
    }
    lv_color = {
        0: "grey",
        1: "white",
        2: "white",
        3: "cyan",
        4: "cyan",
        5: "cyan",
        6: "green",
        7: "green",
        8: "green",
        9: "yellow",
        10: "yellow",
    }

    equip_list = []
    for i, (info, v) in enumerate(u_info):
        if i % 5 == 0:
            equip_table = Table.grid(
                Column("name", width=17, justify="right"),
                Column("value", width=5, justify="left"),
                padding=(0, 1, 0, 0),
            )
        ename, erank = (
            equip_info[info["eid"]]["name"],
            6
            if equip_info[info[f"eid"]]["type"] in [18, 19, 20]
            else equip_info[info["eid"]]["rank"],
        )
        equip_table.add_row(f"[{rank_color[erank]}]{ename}", f"{v.value():2.0f}")
        if (i + 1) % 5 == 0 or i + 1 == len(u_info):
            equip_list.append(equip_table)

    strn_table = Table(
        show_header=False,
        show_lines=True,
        box=box.SQUARE,
        padding=(0, 0, 0, 0),
        title="强化装备",
        title_justify="left",
    )
    for i in range(0, len(equip_list), box_per_row):
        strn_table.add_row(*equip_list[i : min(i + box_per_row, len(equip_list))])

    gun_list = []
    for info, v in g_info:
        gun_table = Table.grid(
            Column("name", width=17, justify="right"),
            Column("value", width=5, justify="left"),
            padding=(0, 1, 0, 0),
        )
        typestr = ["HG", "SMG", "RF", "AR", "MG", "SG"]
        gun_name, gun_type, gun_rank, gun_favor = (
            gun_info[info["gid"]]["name"],
            typestr[gun_info[info["gid"]]["type"] - 1],
            gun_info[info["gid"]]["rank_display"],
            user_gun[info["gid"] % 20000]["favor"],
        )
        gun_table.add_row(
            f"[{rank_color[gun_rank]} bold]{gun_name} [/{rank_color[gun_rank]} bold]{gun_type:<3}",
            f'{"[bold red]o" if gun_favor>100 else "[magenta] "} {gun_favor:3d}',
        )
        # res_table.add_row((f'{gun_name}',gun_type))
        glv, score, slv1, slv2 = (
            user_gun[info["gid"] % 20000]["gun_level"],
            info["score"],
            user_gun[info["gid"] % 20000]["skill1"],
            user_gun[info["gid"] % 20000]["skill2"],
        )
        gun_table.add_row(
            f"[bold][{rank_color[(glv-1)//20+1]}]Lv{glv:>3}[/{rank_color[(glv-1)//20+1]}] [{lv_color[slv1]}]{slv1:2d}"
            + "[white]/"
            + f"[{lv_color[slv2]}]{slv2:2d}",
            f"{score:>5}",
        )
        for e in range(3):
            ename, elv, erank = (
                equip_info[info[f"eid_{e+1}"]]["name"],
                info[f"elv_{e+1}"],
                6
                if equip_info[info[f"eid_{e+1}"]]["type"] in [18, 19, 20]
                else equip_info[info[f"eid_{e+1}"]]["rank"],
            )
            gun_table.add_row(
                f"[{rank_color[erank]}]{ename}", f"[{lv_color[elv]}]{elv:>2}"
            )
        gun_list.append(gun_table)

    res_table = Table(
        show_header=False,
        show_lines=True,
        box=box.SQUARE,
        padding=(0, 0, 0, 0),
        title="出战配置",
        title_justify="left",
    )

    for i in range(0, max_dolls, box_per_row):
        res_table.add_row(*gun_list[i : min(i + box_per_row, max_dolls)])
    full_table = Table(
        show_header=False,
        box=None,
        caption=f"总效能: {resource['score'].value():.0f}",
        caption_justify="left",
    )

    full_table.add_row(strn_table)
    full_table.add_row(res_table)

    console.print(full_table)
