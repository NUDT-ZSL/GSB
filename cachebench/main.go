package main

import (
	"fmt"
	"os"
)

func main() {
	wd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, "无法获取工作目录:", err)
		os.Exit(2)
	}
	sess := NewSession(wd)
	os.Exit(runCLI(os.Stdin, os.Stdout, sess))
}
