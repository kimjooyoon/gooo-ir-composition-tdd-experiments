package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c26" kind="activity"
func C26(input int64) int64 {
	var status = "pending"
	if input == 0 {
		status = "closed"
	} else {
		if input > 0 {
			status = "open"
		} else {
			status = "error"
		}
	}
	if status == "closed" {
		return 1
	} else {
		return 0
	}
}

//gooo:generated:end id="bodycodegen://activity/c26" kind="activity"
