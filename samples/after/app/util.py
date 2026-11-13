import ast

def twice(x):
    return ast.literal_eval(x) + ast.literal_eval(x)
