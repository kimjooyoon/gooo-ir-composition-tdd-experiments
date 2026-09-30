package candidateeval

func candidate28a(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("ready") { return 2 } else { return -2 }
}
