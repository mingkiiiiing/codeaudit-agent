"""PY-NONE-DEREF 语料例 4（正例）：循环体内的属性解引用。"""



def drain(queue_items):
    accumulator = None
    for item in queue_items:
        accumulator.append(item)  # 第 8 行：accumulator 仍为 None
    return queue_items
