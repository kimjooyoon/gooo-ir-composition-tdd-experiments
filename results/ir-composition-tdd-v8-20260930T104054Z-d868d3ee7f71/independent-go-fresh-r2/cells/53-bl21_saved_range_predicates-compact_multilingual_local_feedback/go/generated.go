package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c21" kind="activity"
func C21(input int64) int64 {
	var low = input >= -2
	var high = input <= 2
	var allowed = low && high
	if allowed && true {
		return 1
	} else {
		return 0
	}
}

//gooo:generated:end id="bodycodegen://activity/c21" kind="activity"
