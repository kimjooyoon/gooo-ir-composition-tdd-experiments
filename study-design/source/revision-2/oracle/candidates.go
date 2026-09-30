package oracle

// Candidate functions are exact Go translations of each revision-2 body shell and finite option.

func candidate01a(input int64) int64 {
	if (input >= 3 && input <= 8) { return 2 } else { return -1 }
}

func candidate01b(input int64) int64 {
	if (input > 3 && input <= 8) { return 2 } else { return -1 }
}

func candidate01c(input int64) int64 {
	if (input >= 3 && input < 8) { return 2 } else { return -1 }
}

func candidate02a(input int64) int64 {
	if (input < 0 && input > 0) { return 1 } else { return 0 }
}

func candidate02b(input int64) int64 {
	if (input >= 0) { return 1 } else { return 0 }
}

func candidate02c(input int64) int64 {
	if (input < 0 || input > 0) { return 1 } else { return 0 }
}

func candidate03a(input int64) int64 {
	if (input < -4 || input > 4) { return 7 } else { return 0 }
}

func candidate03b(input int64) int64 {
	if (input == -4 || input == 4) { return 7 } else { return 0 }
}

func candidate03c(input int64) int64 {
	if (input >= -4 && input <= 4) { return 7 } else { return 0 }
}

func candidate04a(input int64) int64 {
	if (input <= -6 || input >= 8) { return 9 } else { return 0 }
}

func candidate04b(input int64) int64 {
	if (input <= -8 || input >= 6) { return 9 } else { return 0 }
}

func candidate04c(input int64) int64 {
	if (input <= -6 && input >= 8) { return 9 } else { return 0 }
}

func candidate05a(input int64) int64 {
	if input < 0 { return -1 } else { if input < 10 { return (input + 2) } else { return 20 } }
}

func candidate05b(input int64) int64 {
	if input < 0 { return -1 } else { if input < 10 { return (input * 3) } else { return 20 } }
}

func candidate05c(input int64) int64 {
	if input < 0 { return -1 } else { if input < 10 { return (input * 2) } else { return 20 } }
}

func candidate06a(input int64) int64 {
	if input < 0 { return 0 } else { if input > 100 { return input - 10 } else { return (input + 11) } }
}

func candidate06b(input int64) int64 {
	if input < 0 { return 0 } else { if input > 100 { return input - 10 } else { return (input + 10) } }
}

func candidate06c(input int64) int64 {
	if input < 0 { return 0 } else { if input > 100 { return input - 10 } else { return (input + 9) } }
}

func candidate07a(input int64) int64 {
	if input >= 0 { if input == 0 { return 5 } else { return 3 } } else { return (input + 2) }
}

func candidate07b(input int64) int64 {
	if input >= 0 { if input == 0 { return 5 } else { return 3 } } else { return (input - 2) }
}

func candidate07c(input int64) int64 {
	if input >= 0 { if input == 0 { return 5 } else { return 3 } } else { return (input + 1) }
}

func candidate08a(input int64) int64 {
	if input < 60 { return -1 } else { if input < 80 { return 0 } else { return (input - 79) } }
}

func candidate08b(input int64) int64 {
	if input < 60 { return -1 } else { if input < 80 { return 0 } else { return (input - 81) } }
}

func candidate08c(input int64) int64 {
	if input < 60 { return -1 } else { if input < 80 { return 0 } else { return (input - 80) } }
}

func candidate09a(input int64) int64 {
	var total = input + 5;
	total = total * 2;
	return (input + 10)
}

func candidate09b(input int64) int64 {
	var total = input + 5;
	total = total * 2;
	return (total)
}

func candidate09c(input int64) int64 {
	var total = input + 5;
	total = total * 2;
	return (input * 2 + 5)
}

func candidate10a(input int64) int64 {
	var adjusted = input;
	if adjusted < 0 { adjusted = 0 } else { adjusted = adjusted + 1 };
	return (adjusted)
}

func candidate10b(input int64) int64 {
	var adjusted = input;
	if adjusted < 0 { adjusted = 0 } else { adjusted = adjusted + 1 };
	return (input)
}

func candidate10c(input int64) int64 {
	var adjusted = input;
	if adjusted < 0 { adjusted = 0 } else { adjusted = adjusted + 1 };
	return (adjusted + 1)
}

func candidate11a(input int64) int64 {
	var score = input * 2;
	var nonnegative = score >= 0;
	if nonnegative { score = score + 1 } else { score = 0 };
	return (input * 2 + 1)
}

func candidate11b(input int64) int64 {
	var score = input * 2;
	var nonnegative = score >= 0;
	if nonnegative { score = score + 1 } else { score = 0 };
	return (input)
}

func candidate11c(input int64) int64 {
	var score = input * 2;
	var nonnegative = score >= 0;
	if nonnegative { score = score + 1 } else { score = 0 };
	return (score)
}

func candidate12a(input int64) int64 {
	var amount = input;
	amount = amount + 3;
	if amount > 10 { amount = 10 } else { amount = amount };
	amount = amount - 1;
	return (amount + 1)
}

func candidate12b(input int64) int64 {
	var amount = input;
	amount = amount + 3;
	if amount > 10 { amount = 10 } else { amount = amount };
	amount = amount - 1;
	return (amount)
}

func candidate12c(input int64) int64 {
	var amount = input;
	amount = amount + 3;
	if amount > 10 { amount = 10 } else { amount = amount };
	amount = amount - 1;
	return (input + 2)
}

func candidate13a(input int64) int64 {
	return 100 - (input + 2) * 3
}

func candidate13b(input int64) int64 {
	return 100 - (input + 3) * 3
}

func candidate13c(input int64) int64 {
	return 100 - (input - 2) * 3
}

func candidate14a(input int64) int64 {
	return (input + (input - 1)) * (input - 2)
}

func candidate14b(input int64) int64 {
	return (input + (input)) * (input - 2)
}

func candidate14c(input int64) int64 {
	return (input + (input + 1)) * (input - 2)
}

func candidate15a(input int64) int64 {
	return input * input - (input - 4)
}

func candidate15b(input int64) int64 {
	return input * input - (input + 4)
}

func candidate15c(input int64) int64 {
	return input * input - (input + 2)
}

func candidate16a(input int64) int64 {
	var next = input + 1;
	return next * (input + 2) - input
}

func candidate16b(input int64) int64 {
	var next = input + 1;
	return next * (input + 1) - input
}

func candidate16c(input int64) int64 {
	var next = input + 1;
	return next * (input + 3) - input
}

func candidate17a(input int64) int64 {
	if (input < 0) { return 1 } else { return 0 }
}

func candidate17b(input int64) int64 {
	if (input == 0) { return 1 } else { return 0 }
}

func candidate17c(input int64) int64 {
	if (input <= 0) { return 1 } else { return 0 }
}

func candidate18a(input int64) int64 {
	if (input == 8) { return 1 } else { return 0 }
}

func candidate18b(input int64) int64 {
	if (input == 7) { return 1 } else { return 0 }
}

func candidate18c(input int64) int64 {
	if (input >= 7) { return 1 } else { return 0 }
}

func candidate19a(input int64) int64 {
	if (input >= -3 && input <= 3 && input != 0) { return 1 } else { return 0 }
}

func candidate19b(input int64) int64 {
	if (input >= -3 && input <= 3) { return 1 } else { return 0 }
}

func candidate19c(input int64) int64 {
	if (input < -3 || input > 3) { return 1 } else { return 0 }
}

func candidate20a(input int64) int64 {
	if (input >= -5 && input <= 5) { return 4 } else { return -4 }
}

func candidate20b(input int64) int64 {
	if (input > -5 && input <= 5) { return 4 } else { return -4 }
}

func candidate20c(input int64) int64 {
	if (input > -5 && input < 5) { return 4 } else { return -4 }
}

func candidate21a(input int64) int64 {
	var low = input >= -2;
	var high = input <= 2;
	var allowed = low && high;
	if allowed && (input != 0) { return 1 } else { return 0 }
}

func candidate21b(input int64) int64 {
	var low = input >= -2;
	var high = input <= 2;
	var allowed = low && high;
	if allowed && (true) { return 1 } else { return 0 }
}

func candidate21c(input int64) int64 {
	var low = input >= -2;
	var high = input <= 2;
	var allowed = low && high;
	if allowed && (input > -2) { return 1 } else { return 0 }
}

func candidate22a(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (valid) { return 5 } else { return -5 }
}

func candidate22b(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (valid || input == 10) { return 5 } else { return -5 }
}

func candidate22c(input int64) int64 {
	var valid = input >= 0;
	valid = valid && input <= 5;
	if (input <= 5) { return 5 } else { return -5 }
}

func candidate23a(input int64) int64 {
	var nonzero = input != 0;
	var bounded = input >= -4 && input <= 4;
	var eligible = nonzero && bounded;
	if eligible && (input != -1) { return 9 } else { return 0 }
}

func candidate23b(input int64) int64 {
	var nonzero = input != 0;
	var bounded = input >= -4 && input <= 4;
	var eligible = nonzero && bounded;
	if eligible && (input > 0) { return 9 } else { return 0 }
}

func candidate23c(input int64) int64 {
	var nonzero = input != 0;
	var bounded = input >= -4 && input <= 4;
	var eligible = nonzero && bounded;
	if eligible && (true) { return 9 } else { return 0 }
}

func candidate24a(input int64) int64 {
	var selected = input == 2;
	if input < 0 { selected = false } else { selected = selected || input == 4 };
	if (input == 2) { return 6 } else { return 0 }
}

func candidate24b(input int64) int64 {
	var selected = input == 2;
	if input < 0 { selected = false } else { selected = selected || input == 4 };
	if (selected) { return 6 } else { return 0 }
}

func candidate24c(input int64) int64 {
	var selected = input == 2;
	if input < 0 { selected = false } else { selected = selected || input == 4 };
	if (selected && input != 4) { return 6 } else { return 0 }
}

func candidate25a(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("negative") { return -1 } else { return 1 }
}

func candidate25b(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("nonnegative") { return -1 } else { return 1 }
}

func candidate25c(input int64) int64 {
	var label = "neutral";
	if input < 0 { label = "negative" } else { label = "nonnegative" };
	if label == ("neutral") { return -1 } else { return 1 }
}

func candidate26a(input int64) int64 {
	var status = "pending";
	if input == 0 { status = "closed" } else { if input > 0 { status = "open" } else { status = "error" } };
	if status == ("closed") { return 1 } else { return 0 }
}

func candidate26b(input int64) int64 {
	var status = "pending";
	if input == 0 { status = "closed" } else { if input > 0 { status = "open" } else { status = "error" } };
	if status == ("error") { return 1 } else { return 0 }
}

func candidate26c(input int64) int64 {
	var status = "pending";
	if input == 0 { status = "closed" } else { if input > 0 { status = "open" } else { status = "error" } };
	if status == ("open") { return 1 } else { return 0 }
}

func candidate27a(input int64) int64 {
	var label = "plum";
	if input < 0 { label = "amber" } else { label = "zinc" };
	if label < ("zoo") { return 1 } else { return 0 }
}

func candidate27b(input int64) int64 {
	var label = "plum";
	if input < 0 { label = "amber" } else { label = "zinc" };
	if label < ("gold") { return 1 } else { return 0 }
}

func candidate27c(input int64) int64 {
	var label = "plum";
	if input < 0 { label = "amber" } else { label = "zinc" };
	if label < ("amber") { return 1 } else { return 0 }
}

func candidate28a(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("ready") { return 2 } else { return -2 }
}

func candidate28b(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("idle-pending") { return 2 } else { return -2 }
}

func candidate28c(input int64) int64 {
	var status = "idle" + "-pending";
	if input > 0 { status = "ready" } else { status = status };
	if status == ("done") { return 2 } else { return -2 }
}

func candidate29a(input int64) int64 {
	if input == 9223372036854775807 { return input } else { return (input) }
}

func candidate29b(input int64) int64 {
	if input == 9223372036854775807 { return input } else { return (input - 1) }
}

func candidate29c(input int64) int64 {
	if input == 9223372036854775807 { return input } else { return (input + 1) }
}

func candidate30a(input int64) int64 {
	if input == (-9223372036854775807 - 1) { return input } else { return (input + 1) }
}

func candidate30b(input int64) int64 {
	if input == (-9223372036854775807 - 1) { return input } else { return (input - 1) }
}

func candidate30c(input int64) int64 {
	if input == (-9223372036854775807 - 1) { return input } else { return (input) }
}

func candidate31a(input int64) int64 {
	if input > 4611686018427387903 { return 9223372036854775807 } else { if input < (-4611686018427387903 - 1) { return (-9223372036854775807 - 1) } else { return (input * 2) } }
}

func candidate31b(input int64) int64 {
	if input > 4611686018427387903 { return 9223372036854775807 } else { if input < (-4611686018427387903 - 1) { return (-9223372036854775807 - 1) } else { return (input * 3) } }
}

func candidate31c(input int64) int64 {
	if input > 4611686018427387903 { return 9223372036854775807 } else { if input < (-4611686018427387903 - 1) { return (-9223372036854775807 - 1) } else { return (input + 2) } }
}

func candidate32a(input int64) int64 {
	var magnitude = input;
	if input < 0 { magnitude = 0 - magnitude } else { magnitude = input };
	if input == (-9223372036854775807 - 1) { return 9223372036854775807 } else { return (input) }
}

func candidate32b(input int64) int64 {
	var magnitude = input;
	if input < 0 { magnitude = 0 - magnitude } else { magnitude = input };
	if input == (-9223372036854775807 - 1) { return 9223372036854775807 } else { return (0 - magnitude) }
}

func candidate32c(input int64) int64 {
	var magnitude = input;
	if input < 0 { magnitude = 0 - magnitude } else { magnitude = input };
	if input == (-9223372036854775807 - 1) { return 9223372036854775807 } else { return (magnitude) }
}
