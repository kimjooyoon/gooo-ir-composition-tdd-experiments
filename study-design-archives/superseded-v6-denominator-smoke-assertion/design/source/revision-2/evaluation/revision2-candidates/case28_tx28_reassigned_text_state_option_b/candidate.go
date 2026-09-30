package candidateeval

func candidate28b(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("idle-pending") { return 2 } else { return -2 }
}
