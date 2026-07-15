GREETING = "Hello, world"
EXCLAMATION_COUNT = 3


def make_greeting(name):
    result = ""
    result = result + GREETING
    result = result + ", " + name
    result = result + "!" * 3
    return result


if __name__ == "__main__":
    print(make_greeting("Themis"))
