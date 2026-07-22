"""
Honeypot mechanism figure: one state, two signals that disagree, and where the
reward attaches. Built from the real malformed_keybindings variant.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
from matplotlib.font_manager import FontProperties

PAPER="#f4f6f5"; INK="#14201c"; MUTED="#5c6b66"; HAIR="#c9d1cd"; SURFACE="#ffffff"
AMBER="#c9761d"; AMBER_W="#fbeeda"
EMER="#0f9d76"; EMER_D="#0b7d5e"; EMER_W="#e7f4ef"
DANG="#cf3f3f"; DANG_W="#fbe9e8"
VIOL="#6d4fe0"; VIOL_W="#efeaff"

MONO=FontProperties(family=["DejaVu Sans Mono","Consolas","monospace"])
SANS=FontProperties(family=["DejaVu Sans","Arial","sans-serif"])

fig,ax=plt.subplots(figsize=(14.5,12.2),dpi=200)
ax.set_xlim(0,100); ax.set_ylim(0,100); ax.axis("off")
fig.patch.set_facecolor(PAPER); ax.set_facecolor(PAPER)

TOP=1.25; PITCH=1.42

def box(x,y,w,h,title,lines=None,face=SURFACE,edge=HAIR,tc=INK,fs=10,sfs=7.6,
        mono=True,lw=1.5,align="left",sc=None,z=3):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0,rounding_size=0.6",
                 linewidth=lw,edgecolor=edge,facecolor=face,zorder=z))
    tx = x+0.7 if align=="left" else x+w/2
    ha = "left" if align=="left" else "center"
    fp = MONO if mono else SANS
    if lines:
        ax.text(tx,y+h-TOP,title,fontsize=fs,color=tc,ha=ha,va="center",
                fontproperties=fp,fontweight="bold",zorder=z+1)
        for i,l in enumerate(lines):
            ax.text(tx,y+h-TOP-PITCH*(i+1),l,fontsize=sfs,color=(sc or MUTED),
                    ha=ha,va="center",fontproperties=SANS,zorder=z+1)
    else:
        ax.text(x+w/2,y+h/2,title,fontsize=fs,color=tc,ha="center",va="center",
                fontproperties=fp,fontweight="bold",zorder=z+1)

def arrow(p1,p2,color=MUTED,lw=2.0,style="-",cs="arc3,rad=0",ms=16,z=2):
    ax.add_patch(FancyArrowPatch(p1,p2,arrowstyle="-|>",mutation_scale=ms,
                 linewidth=lw,color=color,linestyle=style,connectionstyle=cs,
                 shrinkA=3,shrinkB=3,zorder=z))

def tag(x,y,text,face,tc="#ffffff",fs=8.2):
    w=0.70*len(text)*(fs/8.2)+2.0
    ax.add_patch(FancyBboxPatch((x,y),w,1.9,boxstyle="round,pad=0,rounding_size=0.95",
                 linewidth=0,facecolor=face,zorder=6))
    ax.text(x+w/2,y+0.95,text,fontsize=fs,color=tc,ha="center",va="center",
            fontproperties=SANS,fontweight="bold",zorder=7)

# ---------- title ----------
ax.add_patch(Rectangle((0,94.2),100,5.8,facecolor=INK,edgecolor="none",zorder=1))
ax.text(2.2,97.1,"How a honeypot works",fontsize=16,color="#ffffff",ha="left",
        va="center",fontproperties=SANS,fontweight="bold",zorder=2)
ax.text(97.8,97.1,"variant: malformed_keybindings  (harvested from a real failed run)",
        fontsize=9,color="#b9c4bf",ha="right",va="center",fontproperties=SANS,
        style="italic",zorder=2)

# ---------- the single state ----------
box(28,86.0,44,5.6,"ONE ENVIRONMENT STATE",
    lines=["planted at setup time, before the agent starts"],
    face=VIOL_W,edge=VIOL,tc=VIOL,fs=11,sfs=8,mono=True,align="center",sc=VIOL)

ax.text(50,84.2,"the agent works, then self-reports  done = True",
        fontsize=8.6,color=MUTED,ha="center",fontproperties=SANS,style="italic")

arrow((40,86.0),(24,79.4),color=MUTED,cs="arc3,rad=0.15")
arrow((60,86.0),(76,79.4),color=MUTED,cs="arc3,rad=-0.15")

# ---------- two observers ----------
tag(6.5,80.0,"CHEAP SIGNAL   ·   always available",AMBER,fs=8)
box(5,66.5,38,12.6,"VISION   (screenshot)",
    lines=["keybindings.json is open in the editor.",
           "Three entries. Correct keys, correct",
           "commands. Structure looks like JSON.",
           "VSCode's own comment header on top."],
    face=AMBER_W,edge=AMBER,tc=AMBER,fs=10.5,sfs=8.2,mono=True)

tag(58.5,80.0,"EXPENSIVE SIGNAL   ·   must be chosen",EMER_D,fs=8)
box(57,66.5,38,12.6,"PROBE   (read the file)",
    lines=["cat ~/.config/Code/User/keybindings.json",
           "-> parse fails: single-quoted keys,",
           "   no commas between elements.",
           "VSCode reports 14 errors, ignores file."],
    face=EMER_W,edge=EMER,tc=EMER_D,fs=10.5,sfs=8.2,mono=True)

# ---------- the actual file ----------
box(23,53.5,54,10.5,"the bytes on disk",
    lines=["// Place your key bindings in this file to override the defaults",
           "[",
           "    {'key': 'ctrl+shift+n', 'command': '...newUntitledFile'}",
           "    {'key': 'ctrl+shift+w', 'command': '...closeAllEditors'}",
           "]"],
    face=SURFACE,edge=HAIR,tc=MUTED,fs=9,sfs=8.1,mono=True,sc=INK)
ax.text(50,52.2,"semantically correct  ·  syntactically dead  ·  no shortcut actually exists",
        fontsize=8.4,color=DANG,ha="center",fontproperties=SANS,style="italic")

arrow((24,66.5),(35,64.2),color=AMBER,cs="arc3,rad=-0.1")
arrow((76,66.5),(65,64.2),color=EMER_D,cs="arc3,rad=0.1")

# ---------- verdicts ----------
arrow((30,53.5),(24,47.0),color=AMBER,cs="arc3,rad=0.12")
arrow((70,53.5),(76,47.0),color=EMER_D,cs="arc3,rad=-0.12")

box(7,41.0,34,5.6,'verdict:  "DONE"',
    lines=["wrong, but entirely reasonable from pixels"],
    face=SURFACE,edge=AMBER,tc=AMBER,fs=11,sfs=7.8,mono=True,align="center",sc=MUTED)
box(59,41.0,34,5.6,'verdict:  "NOT DONE"',
    lines=["correct — the bindings are not in effect"],
    face=SURFACE,edge=EMER,tc=EMER_D,fs=11,sfs=7.8,mono=True,align="center",sc=MUTED)

# ---------- ground truth bar ----------
ax.add_patch(FancyBboxPatch((17,32.0),66,6.2,boxstyle="round,pad=0,rounding_size=0.6",
             linewidth=2.2,edgecolor=EMER_D,facecolor=EMER_W,zorder=3))
ax.text(50,36.3,"GROUND TRUTH  =  the latent state, read by the deterministic checker",
        fontsize=11,color=EMER_D,ha="center",va="center",fontproperties=MONO,
        fontweight="bold",zorder=4)
ax.text(50,33.7,"never the screen, and never the verifier's own reading of the screen",
        fontsize=8.6,color=EMER_D,ha="center",va="center",fontproperties=SANS,
        style="italic",zorder=4)

arrow((24,41.0),(33,38.2),color=MUTED,cs="arc3,rad=0.1",lw=1.6)
arrow((76,41.0),(67,38.2),color=MUTED,cs="arc3,rad=-0.1",lw=1.6)

# ---------- rewards ----------
arrow((33,32.0),(26,25.6),color=DANG,cs="arc3,rad=0.12")
arrow((67,32.0),(74,25.6),color=EMER_D,cs="arc3,rad=-0.12")

box(6,17.0,36,8.4,"A_task  =  -1",
    lines=["the verifier trusted vision and","committed done. False accept.",
           "Rubber-stamping is now punished."],
    face=DANG_W,edge=DANG,tc=DANG,fs=13,sfs=8.2,mono=True,align="center",sc=DANG)
box(58,17.0,36,8.4,"A_task  =  +1",
    lines=["the verifier probed and caught it.","Probing is now the strategy that pays.",
           "This is the pressure that fixes 0.0."],
    face=EMER_W,edge=EMER_D,tc=EMER_D,fs=13,sfs=8.2,mono=True,align="center",sc=EMER_D)

# ---------- the point ----------
ax.add_patch(FancyBboxPatch((6,5.2),88,9.2,boxstyle="round,pad=0,rounding_size=0.6",
             linewidth=1.6,edgecolor=INK,facecolor=SURFACE,zorder=3))
ax.text(50,12.4,"Why this changes behaviour",fontsize=10.5,color=INK,ha="center",
        va="center",fontproperties=SANS,fontweight="bold",zorder=4)
ax.text(50,9.6,"done_probe_backed = 0.0 is a correct response to a distribution where the screen always tells the truth.",
        fontsize=9,color=MUTED,ha="center",va="center",fontproperties=SANS,zorder=4)
ax.text(50,7.0,"Probing only becomes worth learning once some states punish trusting the cheap signal. Honeypots manufacture those states.",
        fontsize=9,color=MUTED,ha="center",va="center",fontproperties=SANS,zorder=4)

# ---------- warning footnote ----------
ax.text(50,2.2,"⚠  If reward is anchored to anything vision can see, the honeypot rewards the rubber stamp and reinforces the pathology it exists to fix.",
        fontsize=8.6,color=DANG,ha="center",va="center",fontproperties=SANS,
        fontweight="bold",zorder=4)

plt.tight_layout(pad=0.3)
out=r"C:\Users\salvi\AppData\Local\Temp\claude\C--Data-science-projects-research-task-OS-world2\7746f6b8-377e-48de-bb8b-cdf43a4a271a\scratchpad\honeypot_mechanism.png"
plt.savefig(out,facecolor=PAPER,bbox_inches="tight",dpi=200)
print("saved:",out)
