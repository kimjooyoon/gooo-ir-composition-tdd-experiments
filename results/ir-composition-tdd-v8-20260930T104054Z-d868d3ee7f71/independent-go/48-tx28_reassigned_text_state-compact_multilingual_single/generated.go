package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c28" kind="activity"
func C28(input int64) int64 {
	var status = "idle" + "-pending"
	if input > 0 {
		status = "ready"
	} else {
		status = status
	}
	if status == "done" {
		return 2
	} else {
		return -2
	}
}

//gooo:generated:end id="bodycodegen://activity/c28" kind="activity"
