import subprocess

def run(user_input):
    return eval(user_input)

def shell(cmd):
    subprocess.call(cmd, shell=True)
