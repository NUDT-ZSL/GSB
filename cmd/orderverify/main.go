package main

import (
	"fmt"
	"os"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/tools"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var code int
	switch os.Args[1] {
	case "check":
		code = tools.Check("check", os.Args[2:])
	case "fault":
		code = tools.Fault("fault", os.Args[2:])
	case "help", "--help", "-h":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		code = 2
	}
	os.Exit(code)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: orderverify <check|fault> [flags]")
	fmt.Fprintln(os.Stderr, "  check --db output/orderqueue.db --out output/verification-report.json")
	fmt.Fprintln(os.Stderr, "  fault --kind invalid-partition|corrupt-ledger|persistent-failure --out output/fault.json")
	fmt.Fprintln(os.Stderr, "  orderqueue verify|fault provides the same commands without a separate binary")
}
