package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c23" kind="activity"
func C23(input int64) int64 {
	var nonzero = input != 0
	var bounded = input >= -4 && input <= 4
	var eligible = nonzero && bounded
	if eligible && true {
		return 9
	} else {
		return 0
	}
}

//gooo:generated:end id="bodycodegen://activity/c23" kind="activity"
