import ast
import subprocess

def run(user_input):
    return ast.literal_eval(user_input)

def shell(cmd):
    subprocess.call([cmd])
