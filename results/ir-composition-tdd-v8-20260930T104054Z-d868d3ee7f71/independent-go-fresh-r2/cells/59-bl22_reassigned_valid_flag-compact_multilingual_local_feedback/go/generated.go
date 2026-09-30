package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c22" kind="activity"
func C22(input int64) int64 {
	var valid = input >= 0
	valid = valid && input <= 5
	if valid {
		return 5
	} else {
		return -5
	}
}

//gooo:generated:end id="bodycodegen://activity/c22" kind="activity"
