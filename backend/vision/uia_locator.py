import uiautomation as auto
import sys

def find_element(query: str, root=None, timeout: float = 1.0) -> dict | None:
    """
    基于 UIA 的混合检索引擎。
    策略：
    1. 优先搜索顶层窗口 (忽略可见性，确保能找到最小化窗口)。
    2. 其次搜索前台窗口内的控件 (必须可见)。
    3. 支持指定 root 搜索。
    """
    # 强制转换 query 为小写字符串，防止类型错误
    query_norm = str(query).strip().lower()
    with auto.UIAutomationInitializerInThread(debug=False):
        # 策略三：指定 Root 搜索 (如果传入了 root)
        if root:
            return _search_in_root(root, query_norm)

        # 策略一：搜索顶层窗口 (Window Search)
        # 获取桌面下的直接子节点
        desktop = auto.GetRootControl()
        for win in desktop.GetChildren():
            try:
                # 关键：强制获取 Name 并转为字符串，防止 COM 异常或类型问题
                raw_name = win.Name
                if not raw_name:
                    continue
                name = str(raw_name).strip()
                name_norm = name.lower()
                
                # 调试日志 (可选，保留以便排查)
                # print(f"[DEBUG] Checking Window: '{name}'") 

                if query_norm in name_norm:
                    print(f"[MATCH] Hit Window: {name}")
                    # 立即返回窗口信息
                    return _pack_result(win, method="uia_window")
            except Exception:
                continue

        # 策略二：搜索前台控件 (Active Window Control Search)
        try:
            # 获取当前活动窗口
            foreground = auto.GetForegroundControl()
            if foreground:
                top_window = foreground.GetTopLevelControl()
                # print(f"[DEBUG] Searching inside foreground window: {top_window.Name}")
                result = _search_in_root(top_window, query_norm)
                if result:
                    return result
        except Exception:
            pass

        return None

def _search_in_root(root, query_norm):
    """在指定节点下递归搜索可见控件"""
    # 定义过滤条件：可见 + 启用
    # 注意：这里不限制 ControlType，依靠 WalkTree 遍历
    # 深度控制：为了性能，通常不需要遍历太深，但 uiautomation 的 WalkTree 比较快
    
    # 构造一个 Walker，只看可见元素
    condition = auto.PropertyCondition(auto.UIA_IsOffscreenPropertyId, False)
    
    # 使用 FindFirst (深度优先)
    # 注意：FindAll/FindFirst 支持 TreeScope
    # 为了支持模糊匹配，我们可能需要遍历列表。
    # 这里为了简单高效，我们遍历所有 Descendants (有性能风险，但在单个窗口内通常还好)
    
    # 优化：只查找特定的交互型控件，减少数量
    target_types = [
        auto.ControlType.ButtonControl,
        auto.ControlType.EditControl,
        auto.ControlType.MenuItemControl,
        auto.ControlType.TabItemControl,
        auto.ControlType.TextControl,
        auto.ControlType.HyperlinkControl,
        auto.ControlType.ListItemControl,
        auto.ControlType.TreeItemControl,
    ]
    
    type_condition = auto.OrCondition(*[
        auto.PropertyCondition(auto.UIA_ControlTypePropertyId, t) for t in target_types
    ])
    final_condition = auto.AndCondition(condition, type_condition)

    # 查找所有符合类型和可见性的元素
    elements = root.FindAll(auto.TreeScope.Descendants, final_condition)
    
    for element in elements:
        try:
            name = str(element.Name).strip()
            if not name:
                continue
            if query_norm in name.lower():
                print(f"[MATCH] Hit Element: {name} ({element.ControlTypeName})")
                return _pack_result(element, method="uia_control")
        except Exception:
            continue
            
    return None

def _pack_result(element, method="uia"):
    """统一包装返回格式"""
    rect = element.BoundingRectangle
    return {
        "bbox": {
            "x": rect.left,
            "y": rect.top,
            "width": rect.width(),
            "height": rect.height()
        },
        "center": {
            "x": (rect.left + rect.right) // 2,
            "y": (rect.top + rect.bottom) // 2
        },
        "handle": element.NativeWindowHandle,
        "name": element.Name,
        "control_type": element.ControlTypeName,
        "method": method
    }
