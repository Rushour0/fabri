package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
)

func main() {
	input, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, "failed to read stdin:", err)
		os.Exit(1)
	}

	var data map[string]any
	if err := json.Unmarshal(input, &data); err != nil {
		fmt.Fprintln(os.Stderr, "invalid json input:", err)
		os.Exit(1)
	}

	a, _ := data["a"].(float64)
	b, _ := data["b"].(float64)

	out, _ := json.Marshal(map[string]any{"sum": a + b})
	fmt.Println(string(out))
}
