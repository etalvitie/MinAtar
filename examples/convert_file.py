games = ["space_invaders", "asterix", "breakout", "seaquest", "freeway"]
suffixes = ["AC_encoded", "AC", "-k", "-k2"]
for game in games:
    for suffix in suffixes:
        PATH = f"/research/erin/zoshao/results/2022_09_09_{game}_{suffix}.txt"
        result_path = f"/research/erin/zoshao/results/2022_09_09_{game}_{suffix}.results"
        with open(PATH, 'r') as f:
            for l in f:
                sp = l.split()
                r = sp[8]
                frame = sp[11]
                with open(result_path, "a") as d:
                    d.write(str(r)+"\t"+str(frame)+"\n")
                d.close()
        f.close()