package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c31" kind="activity"
func C31(input int64) int64 {
	if input > 4611686018427387903 {
		return 9223372036854775807
	} else {
		if input < (-4611686018427387903 - 1) {
			return (-9223372036854775807 - 1)
		} else {
			return (input * 2)
		}
	}
}

//gooo:generated:end id="bodycodegen://activity/c31" kind="activity"
